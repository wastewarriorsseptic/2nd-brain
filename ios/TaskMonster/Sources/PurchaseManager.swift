import StoreKit
@preconcurrency import WebKit

/// TaskMonster Pro (auto-renewing subscription) via StoreKit 2, driven by the page over the
/// `purchases` script message handler. The page asks for plans / a purchase / a restore; this hands
/// signed transactions (JWS) back to the page, which posts them to the server (/iap/verify/) - the
/// server verifies Apple's signature before granting Pro, so nothing here is trusted by itself.
@MainActor
final class PurchaseManager {
    static let shared = PurchaseManager()

    static let productIDs = ["com.usetaskmonster.pro.yearly", "com.usetaskmonster.pro.monthly"]

    weak var webView: WKWebView?
    private var products: [Product] = []
    private var updatesTask: Task<Void, Never>?

    private init() {}

    /// Renewals, purchases made on another device, and Ask-to-Buy approvals arrive here at any time.
    func startListening() {
        guard updatesTask == nil else { return }
        updatesTask = Task { [weak self] in
            for await result in Transaction.updates {
                await self?.handle(result)
            }
        }
    }

    func handleMessage(_ body: Any) {
        guard let dict = body as? [String: Any], let action = dict["action"] as? String else { return }
        startListening()
        switch action {
        case "products":
            Task { await sendProducts() }
        case "buy":
            if let id = dict["id"] as? String { Task { await buy(id) } }
        case "restore":
            Task { await restore() }
        case "sync":
            Task { await sendCurrentEntitlements() }
        default:
            break
        }
    }

    // MARK: - Products

    private func loadProducts() async {
        if !products.isEmpty { return }
        products = (try? await Product.products(for: Self.productIDs)) ?? []
    }

    private func trialText(for product: Product) async -> String {
        guard let sub = product.subscription,
              let offer = sub.introductoryOffer,
              offer.paymentMode == .freeTrial,
              await sub.isEligibleForIntroOffer else { return "" }
        let n = offer.period.value
        switch offer.period.unit {
        case .day: return "\(n)-day free trial"
        case .week: return n == 1 ? "7-day free trial" : "\(n)-week free trial"
        case .month: return "\(n)-month free trial"
        case .year: return "\(n)-year free trial"
        @unknown default: return ""
        }
    }

    func sendProducts() async {
        await loadProducts()
        var out: [[String: String]] = []
        for p in products.sorted(by: { ($0.subscription?.subscriptionPeriod.unit == .year ? 0 : 1) < ($1.subscription?.subscriptionPeriod.unit == .year ? 0 : 1) }) {
            out.append([
                "id": p.id,
                "displayPrice": p.displayPrice,
                "period": p.subscription?.subscriptionPeriod.unit == .year ? "year" : "month",
                "trial": await trialText(for: p),
            ])
        }
        callPage("window.__iapProducts", json: out)
    }

    // MARK: - Purchasing

    private func buy(_ id: String) async {
        await loadProducts()
        guard let product = products.first(where: { $0.id == id }) else {
            event("error", "That plan isn't available right now.")
            return
        }
        do {
            switch try await product.purchase() {
            case .success(let verification):
                await handle(verification)
            case .userCancelled:
                event("cancelled", "")
            case .pending:
                event("pending", "")
            @unknown default:
                event("error", "Something went wrong - please try again.")
            }
        } catch {
            event("error", "The purchase couldn't be completed.")
        }
    }

    func restore() async {
        do {
            try await AppStore.sync()
        } catch {
            event("error", "Couldn't reach the App Store.")
            return
        }
        await sendCurrentEntitlements()
    }

    func sendCurrentEntitlements() async {
        for await result in Transaction.currentEntitlements {
            if case .verified(let tx) = result, Self.productIDs.contains(tx.productID) {
                callPage("window.__iapTransaction", json: result.jwsRepresentation)
            }
        }
    }

    private func handle(_ result: VerificationResult<Transaction>) async {
        switch result {
        case .verified(let tx):
            // Hand the signed transaction to the page/server first; finishing is safe right away
            // because an active subscription stays in currentEntitlements and is re-sent on launch.
            callPage("window.__iapTransaction", json: result.jwsRepresentation)
            await tx.finish()
        case .unverified:
            event("error", "That purchase couldn't be verified.")
        }
    }

    // MARK: - Page bridge

    private func event(_ type: String, _ message: String) {
        callPage("window.__iapEvent", json: ["type": type, "message": message])
    }

    private func callPage(_ function: String, json value: Any) {
        // JSON fragments (a bare string) need allowFragments-style encoding, so wrap in an array.
        guard let data = try? JSONSerialization.data(withJSONObject: [value]),
              var literal = String(data: data, encoding: .utf8) else { return }
        literal.removeFirst()
        literal.removeLast()
        webView?.evaluateJavaScript("\(function) && \(function)(\(literal));")
    }
}
