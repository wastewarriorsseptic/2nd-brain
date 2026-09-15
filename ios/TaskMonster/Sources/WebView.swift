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
        // Lets the page trigger a real native haptic (UINotificationFeedbackGenerator, via the
        // Coordinator's WKScriptMessageHandler below) on completing a task - reported directly,
        // wanting actual vibration feedback there. The web Vibration API (navigator.vibrate) the
        // page would otherwise reach for has never worked in Safari/WKWebView on iOS at all - it
        // silently does nothing no matter what the page calls, on every iOS version - so
        // window.webkit.messageHandlers.haptics.postMessage(...) from the page's own JS is the
        // only way to reach a real haptic generator from web content on this platform at all.
        config.userContentController.add(context.coordinator, name: "haptics")

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.isOpaque = false
        // Matches the web app's own bg-slate-900 (#0f172a) exactly - this used to be Tailwind's
        // darker slate-950 (#020617) instead, a visibly different shade from the page's actual
        // background. Whenever this native color peeks through the page content - scroll
        // bounce/rubber-banding chief among them, since contentInsetAdjustmentBehavior is .never
        // above - the mismatch showed up as a "haze" band sitting on top of the page, reported
        // directly with several screenshots. No amount of CSS on the page itself could ever have
        // fixed this: it's the native WKWebView's own background paint, not page content.
        webView.backgroundColor = UIColor(red: 0.0588, green: 0.0902, blue: 0.1647, alpha: 1)
        context.coordinator.attach(webView: webView, url: url)
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        private weak var webView: WKWebView?
        private var url: URL?
        private weak var errorOverlay: UIView?

        // Kept alive and pre-.prepare()'d rather than built fresh per call - reported directly as
        // "a slight delay" between tapping and feeling the buzz. A UIFeedbackGenerator has to spin
        // up the Taptic Engine on first use, which is exactly that delay; prepare() pays that cost
        // ahead of time instead of at the moment the page asks for a haptic. See attach() below
        // for the initial warm-up and the didReceive handler for the immediate re-prepare after
        // each fire, so the next tap stays just as fast as the first.
        private let notificationGenerator = UINotificationFeedbackGenerator()
        private let impactGeneratorLight = UIImpactFeedbackGenerator(style: .light)
        private let impactGeneratorMedium = UIImpactFeedbackGenerator(style: .medium)
        private let impactGeneratorHeavy = UIImpactFeedbackGenerator(style: .heavy)

        // Before this, a failed initial load (no connectivity, DNS hiccup, server timeout) left
        // the app sitting on its own background color forever with zero feedback and no way to
        // recover short of force-quitting - reported directly as the app being "just black
        // space" with a screenshot showing exactly this native background and nothing else.
        // WKWebView has no built-in error UI of its own, so this builds a minimal one - message
        // plus a Retry button that just re-issues the same load - and wires it to the
        // didFail/didFailProvisionalNavigation delegate methods below.
        func attach(webView: WKWebView, url: URL) {
            self.webView = webView
            self.url = url

            // Warm the Taptic Engine from app launch, well before the first tap - see the
            // generators' own doc comment above for why this matters.
            notificationGenerator.prepare()
            impactGeneratorLight.prepare()
            impactGeneratorMedium.prepare()
            impactGeneratorHeavy.prepare()

            let overlay = UIView()
            overlay.backgroundColor = webView.backgroundColor
            overlay.isHidden = true
            overlay.translatesAutoresizingMaskIntoConstraints = false

            let label = UILabel()
            label.text = "Couldn't connect. Check your internet connection and try again."
            label.textColor = .white
            label.numberOfLines = 0
            label.textAlignment = .center
            label.font = .systemFont(ofSize: 16, weight: .medium)
            label.translatesAutoresizingMaskIntoConstraints = false

            let retryButton = UIButton(type: .system)
            retryButton.setTitle("Retry", for: .normal)
            retryButton.setTitleColor(.white, for: .normal)
            retryButton.titleLabel?.font = .systemFont(ofSize: 16, weight: .bold)
            retryButton.backgroundColor = UIColor(red: 0.302, green: 0.267, blue: 0.851, alpha: 1) // indigo-600
            retryButton.layer.cornerRadius = 12
            retryButton.contentEdgeInsets = UIEdgeInsets(top: 12, left: 28, bottom: 12, right: 28)
            retryButton.translatesAutoresizingMaskIntoConstraints = false
            retryButton.addTarget(self, action: #selector(retryTapped), for: .touchUpInside)

            overlay.addSubview(label)
            overlay.addSubview(retryButton)
            webView.addSubview(overlay)

            NSLayoutConstraint.activate([
                overlay.leadingAnchor.constraint(equalTo: webView.leadingAnchor),
                overlay.trailingAnchor.constraint(equalTo: webView.trailingAnchor),
                overlay.topAnchor.constraint(equalTo: webView.topAnchor),
                overlay.bottomAnchor.constraint(equalTo: webView.bottomAnchor),

                label.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
                label.centerYAnchor.constraint(equalTo: overlay.centerYAnchor, constant: -28),
                label.leadingAnchor.constraint(greaterThanOrEqualTo: overlay.leadingAnchor, constant: 32),
                label.trailingAnchor.constraint(lessThanOrEqualTo: overlay.trailingAnchor, constant: -32),

                retryButton.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 20),
                retryButton.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            ])

            errorOverlay = overlay
        }

        @objc private func retryTapped() {
            guard let webView, let url else { return }
            errorOverlay?.isHidden = true
            webView.load(URLRequest(url: url))
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            errorOverlay?.isHidden = true
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            showErrorOverlayUnlessCancelled(error)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            showErrorOverlayUnlessCancelled(error)
        }

        // Cancelling a navigation from decidePolicyFor (every external link/OAuth redirect handed
        // off to Safari via UIApplication.shared.open above) delivers a didFailProvisionalNavigation
        // with NSURLErrorCancelled (-999) right along with the real network-failure case - without
        // this guard, tapping any external link or going through Google/Apple sign-in would flash
        // the "Couldn't connect" retry screen even though nothing actually failed.
        private func showErrorOverlayUnlessCancelled(_ error: Error) {
            if (error as NSError).code == NSURLErrorCancelled { return }
            errorOverlay?.isHidden = false
        }

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

        // Fires on window.webkit.messageHandlers.haptics.postMessage(...) from the page - see the
        // userContentController.add(...) registration in makeUIView above. The page sends a kind
        // string ("success"/"warning"/"error" for UINotificationFeedbackGenerator, anything else
        // treated as an impact style) so it can ask for different feedback in different spots
        // without another native round-trip later.
        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard message.name == "haptics" else { return }
            let kind = (message.body as? String) ?? "success"
            switch kind {
            case "success":
                notificationGenerator.notificationOccurred(.success)
                notificationGenerator.prepare()
            case "warning":
                notificationGenerator.notificationOccurred(.warning)
                notificationGenerator.prepare()
            case "error":
                notificationGenerator.notificationOccurred(.error)
                notificationGenerator.prepare()
            case "light":
                impactGeneratorLight.impactOccurred()
                impactGeneratorLight.prepare()
            case "heavy":
                impactGeneratorHeavy.impactOccurred()
                impactGeneratorHeavy.prepare()
            default:
                impactGeneratorMedium.impactOccurred()
                impactGeneratorMedium.prepare()
            }
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
