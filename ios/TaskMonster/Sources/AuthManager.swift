import AuthenticationServices
@preconcurrency import WebKit

/// Routes Google/Apple sign-in through ASWebAuthenticationSession instead of this app's own
/// WKWebView - see WebView.swift's decidePolicyFor, which detects a tap on "Sign in with Google"/
/// "Sign in with Apple" (a navigation to /login or /login/apple) and calls startSignIn here instead
/// of letting that navigation load in-WebView. Reported directly as a black screen with no error
/// and no way forward: Google (and, less aggressively, Apple) actively block OAuth sign-in attempts
/// from a generic embedded WebView - the previous approach of just keeping the whole flow inside
/// the WKWebView (see that file's own inAppHosts comment, added 2026-09-25) worked for a while but
/// was always fighting that policy rather than working with it.
///
/// ASWebAuthenticationSession opens its own system-managed, Safari-backed session that completes
/// the OAuth round trip the way Google/Apple actually expect, then hands this app the final
/// redirect URL directly - matched against the "taskmonster://" scheme registered in Info.plist -
/// without ever actually loading it anywhere. No other app can intercept that callback: iOS
/// delivers it in-process to whichever app's session is waiting on it, not through the general
/// URL-open mechanism a plain custom-scheme deep link would go through.
///
/// That session has its own separate cookie jar from the WKWebView's own, though, so completing
/// sign-in there doesn't by itself log this app's own WebView in - see /auth/native-login's
/// docstring in app.py for the short-lived signed-token handoff that finishes the job by loading
/// one more URL in the real WKWebView once the callback above fires.
@MainActor
final class AuthManager: NSObject {
    static let shared = AuthManager()

    private var session: ASWebAuthenticationSession?

    private override init() { super.init() }

    /// provider is "google" or "apple" - matches this app's own /login vs /login/apple routes.
    /// native=1 tells the backend this sign-in is happening through ASWebAuthenticationSession so
    /// its callback should hand back a one-time token via the custom URL scheme instead of
    /// redirecting to "/" the way a plain web sign-in does.
    func startSignIn(provider: String, in webView: WKWebView) {
        let path = provider == "apple" ? "/login/apple" : "/login"
        guard let url = URL(string: "https://usetaskmonster.app\(path)?native=1") else { return }

        let authSession = ASWebAuthenticationSession(
            url: url,
            callbackURLScheme: "taskmonster"
        ) { [weak self, weak webView] callbackURL, _ in
            guard let callbackURL, let webView else { return }
            self?.finishSignIn(callbackURL: callbackURL, in: webView)
        }
        authSession.presentationContextProvider = self
        // Sharing Safari's own cookie jar (not an ephemeral/isolated one) matters specifically for
        // 2FA/"verify it's you" steps mid-flow - an already-trusted browser session on this device
        // can skip straight past a challenge an isolated one would otherwise have to re-prove, the
        // same reasoning WebView.swift's own inAppHosts comment gives for why those extra
        // verification hosts matter at all.
        authSession.prefersEphemeralWebBrowserSession = false
        session = authSession
        authSession.start()
    }

    private func finishSignIn(callbackURL: URL, in webView: WKWebView) {
        guard let token = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false)?
            .queryItems?.first(where: { $0.name == "token" })?.value
        else { return }

        var components = URLComponents(string: "https://usetaskmonster.app/auth/native-login")!
        components.queryItems = [URLQueryItem(name: "token", value: token)]
        guard let completeURL = components.url else { return }

        var request = URLRequest(url: completeURL)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        webView.load(request)
    }
}

extension AuthManager: ASWebAuthenticationPresentationContextProviding {
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        UIApplication.shared.connectedScenes
            .compactMap { ($0 as? UIWindowScene)?.keyWindow }
            .first ?? ASPresentationAnchor()
    }
}
