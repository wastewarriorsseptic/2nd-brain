import SwiftUI
@preconcurrency import WebKit
import Speech
import AVFoundation

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
        // The page's own mic button used to call the browser's webkitSpeechRecognition API
        // directly - that constructor exists inside WKWebView and .start() never throws, but it
        // never actually captures or transcribes any audio there (unlike real Mobile Safari,
        // where the same API works fine) - reported directly, twice: the mic button visibly went
        // into its "listening" state but nothing was ever transcribed, even after confirming the
        // Info.plist NSSpeechRecognitionUsageDescription fix had already shipped. This message
        // handler lets the page hand recognition off to this bridge instead, which drives Apple's
        // own Speech framework directly - see startSpeechRecognition/stopSpeechRecognition below.
        config.userContentController.add(context.coordinator, name: "speechRecognition")
        // TaskMonster Pro subscription (StoreKit 2) - see PurchaseManager.
        config.userContentController.add(context.coordinator, name: "purchases")

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        // Reported directly with a screenshot: pulling down past the top of a page (the Notes
        // page's own scroll view, but this is a single shared WKWebView so it applied everywhere)
        // rubber-banded past the content edge and revealed empty native background above it, which
        // read as a stray "pull to refresh"-looking gap even though nothing was actually bound to
        // that gesture. Disabling the scroll view's elastic bounce entirely removes the gesture's
        // visible effect at both edges - there's nothing left to pull past.
        webView.scrollView.bounces = false
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
        PurchaseManager.shared.webView = webView
        // .reloadIgnoringLocalCacheData, not the default .useProtocolCachePolicy - the site's own
        // response carries no Cache-Control/Last-Modified headers at all, which is exactly the
        // condition under which NSURLCache applies its own heuristic freshness lifetime instead of
        // treating the response as always-revalidate. That meant a real risk of this WKWebView
        // quietly serving yesterday's index.html/JS after a fresh TestFlight install - three
        // separate real fixes in a row (the Speech Recognition permission, the native bridge
        // replacing the browser API, the AVAudioEngine crash guard) each landed on the server with
        // zero change in the reported symptom, which only makes sense if none of them were
        // actually reaching the device at all. Every load this app ever does is of content that
        // must be current - there's no scenario where a stale cached copy of this page is
        // preferable to a fresh network fetch - so bypass the local cache unconditionally.
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        webView.load(request)
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
        // .rigid is a single sharp knock with essentially no ramp-up, vs. notificationOccurred's
        // built-in two-pulse "success" pattern which takes longer to fully play out by design (it's
        // meant to feel ceremonial, not instantaneous) - asked for something snappier after the
        // prepare()/ordering latency fix already landed, so this is the completion haptic's default now.
        private let impactGeneratorRigid = UIImpactFeedbackGenerator(style: .rigid)

        // Drives on-device speech-to-text directly via Apple's own Speech framework, since
        // WKWebView's webkitSpeechRecognition constructor exists but never actually transcribes
        // anything there (see the config.userContentController.add(... "speechRecognition") call
        // in makeUIView for the full story). One recognizer instance reused across sessions;
        // request/task/audio engine are torn down and rebuilt each time recognition starts, since
        // a used SFSpeechAudioBufferRecognitionRequest can't be restarted.
        private let speechRecognizer = SFSpeechRecognizer(locale: Locale.current)
        private var speechRecognitionRequest: SFSpeechAudioBufferRecognitionRequest?
        private var speechRecognitionTask: SFSpeechRecognitionTask?
        private let speechAudioEngine = AVAudioEngine()

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
            impactGeneratorRigid.prepare()

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
            var request = URLRequest(url: url)
            request.cachePolicy = .reloadIgnoringLocalCacheData
            webView.load(request)
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
        //
        // Only listing "accounts.google.com" itself missed a real report: a friend testing the
        // app "couldn't login" - the account-owner's own Google account (used for the 2026-09-04
        // verification above) has no 2-Step Verification challenge and is already a trusted
        // device, so it never left accounts.google.com. A account with 2FA, a "verify it's you"
        // security check, or a new/unrecognized-device prompt gets redirected through OTHER
        // Google subdomains (myaccount.google.com and similar) that weren't in this list - the
        // WKWebView would bounce that navigation out to Safari mid-flow instead of completing it
        // in-app, so the user finishes signing in in Safari while the app's own WKWebView (a
        // separate, non-shared cookie store) never receives the resulting session and just sits
        // on the login screen. Matching any *.google.com / *.apple.com host (not just the one
        // subdomain each provider's sign-in NORMALLY uses) covers those extra verification steps
        // too, without opening this up to unrelated sites.
        private let inAppHosts: Set<String> = [
            "usetaskmonster.app",
            "google.com",
            "apple.com",
        ]

        // Matching all of google.com/apple.com above (not just the accounts.* subdomain each
        // provider's sign-in NORMALLY uses) would also pull an ordinary content link - a Google
        // Doc or Drive file pasted into a task's own title/description, say - into this chromeless
        // WKWebView instead of handing it to Safari as before, with no address bar or back button
        // to escape it. These are the well-known non-auth content subdomains most likely to show
        // up as a link IN a task rather than as part of a sign-in redirect; carved back out so
        // that behavior for them is unchanged.
        private let contentSubdomainExceptions: Set<String> = [
            "docs.google.com", "drive.google.com", "sheets.google.com", "slides.google.com",
            "forms.google.com", "calendar.google.com", "mail.google.com", "photos.google.com",
            "maps.google.com", "meet.google.com", "keep.google.com", "translate.google.com",
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
            let isInApp = inAppHosts.contains(where: { host == $0 || host.hasSuffix(".\($0)") })
                && !contentSubdomainExceptions.contains(host)
            if isInApp {
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

        // Fires on window.webkit.messageHandlers.<name>.postMessage(...) from the page - see the
        // two config.userContentController.add(...) registrations in makeUIView above.
        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            switch message.name {
            case "haptics":
                // The page sends a kind string ("success"/"warning"/"error" for
                // UINotificationFeedbackGenerator, anything else treated as an impact style) so it
                // can ask for different feedback in different spots without another native round
                // trip later.
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
                case "rigid":
                    impactGeneratorRigid.impactOccurred()
                    impactGeneratorRigid.prepare()
                default:
                    impactGeneratorMedium.impactOccurred()
                    impactGeneratorMedium.prepare()
                }

            case "purchases":
                Task { @MainActor in PurchaseManager.shared.handleMessage(message.body) }

            case "speechRecognition":
                let command = (message.body as? String) ?? ""
                if command == "stop" {
                    stopSpeechRecognition()
                } else {
                    startSpeechRecognition()
                }

            default:
                break
            }
        }

        // MARK: - Speech recognition bridge
        //
        // The page's mic button posts "start"/"stop" here instead of calling the browser's own
        // webkitSpeechRecognition, which exists inside WKWebView but never actually transcribes
        // anything there. This drives Apple's Speech framework directly and calls back into the
        // page's own JS (window.__nativeSpeechResult/__nativeSpeechEnded, defined in index.html's
        // toggleAiChatDictation rewrite) with the same shape the old browser API's onresult/onend
        // callbacks already provided, so the rest of that dictation code - accumulating final text,
        // showing interim text live, auto-growing the textarea - needed no changes.
        private func startSpeechRecognition() {
            guard let speechRecognizer, speechRecognizer.isAvailable else {
                sendSpeechEndedToPage()
                return
            }

            SFSpeechRecognizer.requestAuthorization { [weak self] authStatus in
                DispatchQueue.main.async {
                    guard let self else { return }
                    guard authStatus == .authorized else {
                        self.sendSpeechEndedToPage()
                        return
                    }
                    AVAudioSession.sharedInstance().requestRecordPermission { granted in
                        DispatchQueue.main.async {
                            if granted {
                                self.beginSpeechAudioCapture()
                            } else {
                                self.sendSpeechEndedToPage()
                            }
                        }
                    }
                }
            }
        }

        private func beginSpeechAudioCapture() {
            // Tear down any previous session first - a used recognitionRequest/task can't be
            // restarted, and the audio engine's own tap can only ever have one installed at a time.
            stopSpeechRecognition(silently: true)

            let audioSession = AVAudioSession.sharedInstance()
            do {
                try audioSession.setCategory(.record, mode: .measurement, options: .duckOthers)
                try audioSession.setActive(true, options: .notifyOthersOnDeactivation)
            } catch {
                sendSpeechEndedToPage()
                return
            }

            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            // On-device only - keeps dictation working with no network round trip and matches the
            // app's own "your data stays on your phone" posture for voice input specifically.
            if #available(iOS 13.0, *) {
                request.requiresOnDeviceRecognition = speechRecognizer?.supportsOnDeviceRecognition ?? false
            }
            speechRecognitionRequest = request

            let inputNode = speechAudioEngine.inputNode
            let recordingFormat = inputNode.outputFormat(forBus: 0)
            // installTap crashes outright (a hard precondition inside AVAudioEngine, not a
            // catchable Swift error) if the format it's given has a zero sample rate or channel
            // count - a real, well-documented race where the input node's format hasn't finished
            // settling yet right after the audio session was just activated above. Bailing out
            // cleanly here instead of crashing matches "can't even tap the mic button anymore"
            // reported directly - a crash here would leave the WebView's whole JS environment
            // wedged until the app is force-quit and relaunched, which reads exactly like the
            // button silently stopped responding at all, not just failing to transcribe.
            guard recordingFormat.sampleRate > 0, recordingFormat.channelCount > 0 else {
                sendSpeechEndedToPage()
                return
            }
            inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { buffer, _ in
                request.append(buffer)
            }

            speechAudioEngine.prepare()
            do {
                try speechAudioEngine.start()
            } catch {
                sendSpeechEndedToPage()
                return
            }

            speechRecognitionTask = speechRecognizer?.recognitionTask(with: request) { [weak self] result, error in
                guard let self else { return }
                if let result {
                    self.sendSpeechResultToPage(
                        transcript: result.bestTranscription.formattedString,
                        isFinal: result.isFinal
                    )
                    if result.isFinal {
                        self.stopSpeechRecognition()
                    }
                }
                if error != nil {
                    self.stopSpeechRecognition()
                }
            }
        }

        private func stopSpeechRecognition(silently: Bool = false) {
            if speechAudioEngine.isRunning {
                speechAudioEngine.stop()
                speechRecognitionRequest?.endAudio()
            }
            // removeTap is documented as safe to call even when no tap is installed - always
            // calling it (rather than trying to track "was one installed" separately) is simpler
            // and guarantees a clean slate for the input node's format to be re-read fresh next
            // time beginSpeechAudioCapture runs, which is exactly what the sampleRate/channelCount
            // guard there is trying to protect against.
            speechAudioEngine.inputNode.removeTap(onBus: 0)
            speechAudioEngine.reset()
            speechRecognitionTask?.cancel()
            speechRecognitionTask = nil
            speechRecognitionRequest = nil
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            if !silently {
                sendSpeechEndedToPage()
            }
        }

        private func sendSpeechResultToPage(transcript: String, isFinal: Bool) {
            let escaped = transcript
                .replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"")
                .replacingOccurrences(of: "\n", with: "\\n")
            let js = "window.__nativeSpeechResult && window.__nativeSpeechResult(\"\(escaped)\", \(isFinal));"
            webView?.evaluateJavaScript(js)
        }

        private func sendSpeechEndedToPage() {
            webView?.evaluateJavaScript("window.__nativeSpeechEnded && window.__nativeSpeechEnded();")
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
