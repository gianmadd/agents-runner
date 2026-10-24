// A native window around the fns page, nothing else: workers.py serves the page, compiles this once and opens it with the URL.
import AppKit
import WebKit

final class Window: NSObject, NSApplicationDelegate, NSWindowDelegate, WKUIDelegate {
    let url: URL
    var window: NSWindow!

    init(url: URL) { self.url = url }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let web = WKWebView(frame: .zero)
        web.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 880),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Start a session"
        window.contentView = web
        window.delegate = self
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()  // in front even if macOS refuses to activate an app started from a terminal
        NSApp.activate()
        web.load(URLRequest(url: url))
    }

    // window.close() from the page after Start, or the user closing the window (workers.py then cancels).
    func webViewDidClose(_ webView: WKWebView) { NSApp.terminate(nil) }
    func windowWillClose(_ notification: Notification) { NSApp.terminate(nil) }
}

// Without a main menu, Cmd-C/V/A/Q do nothing in the role name fields.
func mainMenu() -> NSMenu {
    let menu = NSMenu(), app = NSMenuItem(), edit = NSMenuItem()
    app.submenu = NSMenu()
    app.submenu!.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    edit.submenu = NSMenu(title: "Edit")
    for (title, action, key) in [("Cut", #selector(NSText.cut(_:)), "x"), ("Copy", #selector(NSText.copy(_:)), "c"),
                                 ("Paste", #selector(NSText.paste(_:)), "v"), ("Select All", #selector(NSText.selectAll(_:)), "a")] {
        edit.submenu!.addItem(withTitle: title, action: action, keyEquivalent: key)
    }
    menu.addItem(app)
    menu.addItem(edit)
    return menu
}

guard CommandLine.arguments.count > 1, let url = URL(string: CommandLine.arguments[1]) else {
    FileHandle.standardError.write("usage: window <url>\n".data(using: .utf8)!)
    exit(2)
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // a passing panel: no Dock icon, so nothing lingers among the Dock's recent apps
app.mainMenu = mainMenu()
let delegate = Window(url: url)
app.delegate = delegate
app.run()
