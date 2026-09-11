import SwiftUI
@preconcurrency import WebKit

/// Wraps the live TaskMonster site (server-rendered FastAPI/Jinja app - there is no bundled
/// local copy) in a WKWebView. The default WKWebsiteDataStore persists cookies across launches
/// on its own, so a signed-in session survives app relaunches with no extra code.
struct WebView: UIViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        // The AI chat panel's mic button uses getUserMedia() for voice dictation - iOS 15+
        // WKWebView needs this to grant that permission (see Coordinator's WKUIDelegate method
        // below, plus NSMicrophoneUsageDescription in Info.plist).
        config.mediaTypesRequiringUserActionForPlayback = []

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.isOpaque = false
        webView.backgroundColor = UIColor(red: 0.008, green: 0.024, blue: 0.09, alpha: 1)
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        // Known non-usetaskmonster.app hosts that are still part of a legitimate in-flow
        // navigation (Google/Apple sign-in) rather than an external link - let these load
        // in the same WKWebView instead of bouncing out to Safari.
        // Verified end-to-end on a real device/simulator with a real Google account (2026-09-04):
        // the full sign-in flow, including the password/consent steps, completes normally here.
        // Google's "disallowed_useragent" block targets WKWebView sessions using a stripped-down
        // custom user agent or other automation signals - this WKWebView uses its stock default
        // configuration/user agent, which is why it isn't flagged.
        private let inAppHosts: Set<String> = [
            "usetaskmonster.app",
            "accounts.google.com",
            "appleid.apple.com",
        ]

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url, let host = url.host else {
                decisionHandler(.allow)
                return
            }
            if inAppHosts.contains(where: { host == $0 || host.hasSuffix(".\($0)") }) {
                decisionHandler(.allow)
            } else {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
            }
        }

        // target="_blank" links (e.g. the Amazon "shoppable" links) - WKWebView has no popup
        // window of its own, so hand these to Safari instead of silently dropping them.
        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let url = navigationAction.request.url {
                UIApplication.shared.open(url)
            }
            return nil
        }

        func webView(
            _ webView: WKWebView,
            requestMediaCapturePermissionFor origin: WKSecurityOrigin,
            initiatedByFrame frame: WKFrameInfo,
            type: WKMediaCaptureType,
            decisionHandler: @escaping (WKPermissionDecision) -> Void
        ) {
            decisionHandler(.grant)
        }

        // WKUIDelegate's alert/confirm/prompt panel methods are all optional - leaving them
        // unimplemented doesn't fall back to some default system dialog, it silently no-ops
        // instead: an unhandled alert() just never shows anything, and an unhandled confirm()
        // immediately completes with `false` as if the user tapped Cancel, with no UI at all.
        // That made every confirm() in the web app - Delete Account chief among them, reported
        // directly after "tapped the button" produced nothing - silently do nothing instead of
        // asking, inside this native wrapper specifically (the same page works fine in Safari).
        // These three implementations route each JS dialog through a real UIAlertController on
        // the topmost presented view controller instead.
        func webView(
            _ webView: WKWebView,
            runJavaScriptAlertPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping () -> Void
        ) {
            guard let presenter = Self.topViewController() else { completionHandler(); return }
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
            presenter.present(alert, animated: true)
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping (Bool) -> Void
        ) {
            guard let presenter = Self.topViewController() else { completionHandler(false); return }
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
            presenter.present(alert, animated: true)
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptTextInputPanelWithPrompt prompt: String,
            defaultText: String?,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping (String?) -> Void
        ) {
            guard let presenter = Self.topViewController() else { completionHandler(nil); return }
            let alert = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
            alert.addTextField { $0.text = defaultText }
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(nil) })
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in
                completionHandler(alert.textFields?.first?.text)
            })
            presenter.present(alert, animated: true)
        }

        // This app has no navigation/tab chrome of its own (ContentView is just the WebView), but
        // walks through presented/nav/tab controllers anyway so a JS dialog fired while some other
        // sheet is already up still lands on top of it instead of failing to present.
        private static func topViewController(base: UIViewController? = {
            UIApplication.shared.connectedScenes
                .compactMap { ($0 as? UIWindowScene)?.keyWindow }
                .first?.rootViewController
        }()) -> UIViewController? {
            if let nav = base as? UINavigationController {
                return topViewController(base: nav.visibleViewController)
            }
            if let tab = base as? UITabBarController {
                return topViewController(base: tab.selectedViewController)
            }
            if let presented = base?.presentedViewController {
                return topViewController(base: presented)
            }
            return base
        }
    }
}
