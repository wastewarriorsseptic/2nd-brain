import SwiftUI

struct ContentView: View {
    private let siteURL = URL(string: "https://usetaskmonster.app")!

    var body: some View {
        WebView(url: siteURL)
            .background(Color.black)
    }
}
