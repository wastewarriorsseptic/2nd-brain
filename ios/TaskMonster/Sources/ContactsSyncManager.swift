import Contacts
import ContactsUI
@preconcurrency import WebKit

/// One-way iPhone Contacts sync for TaskMonster's People page, driven by the page over the
/// `contactsSync` script message handler (see WebView.swift's makeUIView/didReceive). The phone is
/// always the source of truth: this only ever reads from CNContactStore and pushes what it finds to
/// /people/sync/ (via the page's own window.__contactsPicked callback) - it never writes anything
/// back to the user's actual Contacts app. Scoped per-Universe: the page always sends a universe_id
/// with a pick request, and tracked identifiers are stored per-Universe so a later background
/// refresh only touches the contacts that Universe actually picked.
@MainActor
final class ContactsSyncManager: NSObject {
    static let shared = ContactsSyncManager()

    weak var webView: WKWebView?
    private let store = CNContactStore()
    private var activePicker: ContactPickerCoordinator?

    private override init() { super.init() }

    // MARK: - Page -> native

    func handleMessage(_ body: Any) {
        guard let dict = body as? [String: Any], let action = dict["action"] as? String else { return }
        switch action {
        case "pick":
            let universeId = (dict["universe_id"] as? NSNumber)?.intValue ?? (dict["universe_id"] as? Int)
            guard let universeId else { return }
            Task { await requestAccessThenPresentPicker(universeId: universeId) }
        default:
            break
        }
    }

    // MARK: - Permission + picker

    private func requestAccessThenPresentPicker(universeId: Int) async {
        let status = CNContactStore.authorizationStatus(for: .contacts)
        switch status {
        case .authorized:
            presentPicker(universeId: universeId)
        case .notDetermined:
            let granted = (try? await store.requestAccess(for: .contacts)) ?? false
            if granted { presentPicker(universeId: universeId) }
            // A denial here is a silent no-op, same as the Speech Recognition bridge's own
            // sendSpeechEndedToPage precedent - the page's own feature-detection already hid the
            // CTA if this could never work, so there's nothing useful to tell it beyond "nothing
            // happened".
        default:
            break
        }
    }

    private func presentPicker(universeId: Int) {
        guard let presenter = Self.topViewController() else { return }
        let picker = CNContactPickerViewController()
        picker.predicateForEnablingContact = NSPredicate(format: "phoneNumbers.@count > 0 OR emailAddresses.@count > 0")
        picker.displayedPropertyKeys = [
            CNContactPhoneNumbersKey, CNContactEmailAddressesKey,
        ]
        let coordinator = ContactPickerCoordinator(universeId: universeId) { [weak self] contacts in
            self?.activePicker = nil
            guard !contacts.isEmpty else { return }
            self?.pushToPage(contacts: contacts, universeId: universeId)
        }
        picker.delegate = coordinator
        // Held on self so it isn't deallocated mid-presentation (CNContactPickerViewController
        // only holds its delegate weakly).
        activePicker = coordinator
        presenter.present(picker, animated: true)
    }

    // MARK: - Native -> page

    private func pushToPage(contacts: [CNContact], universeId: Int) {
        let payload: [String: Any] = [
            "universe_id": universeId,
            "contacts": contacts.map(Self.jsonDict(for:)),
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let literal = String(data: data, encoding: .utf8) else { return }
        webView?.evaluateJavaScript("window.__contactsPicked && window.__contactsPicked(\(literal));")
    }

    private static func jsonDict(for contact: CNContact) -> [String: String] {
        let name = CNContactFormatter.string(from: contact, style: .fullName)
            ?? "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces)
        var dict: [String: String] = [
            "apple_contact_id": contact.identifier,
            "name": name.isEmpty ? "Unnamed Contact" : name,
        ]
        if let phone = contact.phoneNumbers.first?.value.stringValue {
            dict["phone"] = phone
        }
        if let email = contact.emailAddresses.first?.value as String? {
            dict["email"] = email
        }
        return dict
    }

    // Same "walk through whatever's presented" lookup WebView.swift's own topViewController uses
    // for JS alert/confirm/prompt panels - kept as its own copy here rather than exposing that
    // private helper across files, matching how PurchaseManager.swift stays fully self-contained.
    private static func topViewController(base: UIViewController? = {
        UIApplication.shared.connectedScenes
            .compactMap { ($0 as? UIWindowScene)?.keyWindow }
            .first?.rootViewController
    }()) -> UIViewController? {
        if let nav = base as? UINavigationController {
            return topViewController(base: nav.visibleViewController)
        }
        if let tab = base as? UITabBarController, let selected = tab.selectedViewController {
            return topViewController(base: selected)
        }
        if let presented = base?.presentedViewController {
            return topViewController(base: presented)
        }
        return base
    }
}

/// CNContactPickerViewController holds its delegate weakly, so this tiny adapter is what actually
/// survives for the duration of the picker being on screen (ContactsSyncManager holds a strong
/// reference to it via activePicker, cleared once the callback fires).
private final class ContactPickerCoordinator: NSObject, CNContactPickerDelegate {
    let universeId: Int
    let onPicked: ([CNContact]) -> Void

    init(universeId: Int, onPicked: @escaping ([CNContact]) -> Void) {
        self.universeId = universeId
        self.onPicked = onPicked
    }

    // Implementing ONLY the plural (multi-select) delegate method, not the singular
    // didSelect(contact:) variant too - CNContactPickerViewController checks which of the two
    // the delegate responds to in order to decide whether to show its own checkbox multi-select
    // UI at all. Implementing both would make that ambiguous; this app always wants multi-select
    // (picking several contacts to sync at once, not one at a time), matching the "pick specific
    // contacts, not the whole address book" design.
    func contactPicker(_ picker: CNContactPickerViewController, didSelect contacts: [CNContact]) {
        onPicked(contacts)
    }

    func contactPickerDidCancel(_ picker: CNContactPickerViewController) {
        onPicked([])
    }
}
