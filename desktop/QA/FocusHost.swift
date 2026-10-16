// Artificial local document for native material/focus/click-through checks. No data sources.
import AppKit
final class CornerTarget: NSView {
    override func mouseDown(with event: NSEvent) { (superview as? HostView)?.backgroundClick() }
}
final class HostView: NSView {
    var count = 0
    let counter = NSTextField(labelWithString: "背景点击 0")
    let marker = CornerTarget(frame: NSRect(x: 0, y: 0, width: 4, height: 4))
    var markerTimer: Timer?
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.white.cgColor
        counter.textColor = .black
        counter.frame = NSRect(x: 20, y: 15, width: 240, height: 30)
        addSubview(counter)
        marker.wantsLayer = true; marker.layer?.backgroundColor = NSColor.systemRed.cgColor
        marker.layer?.cornerRadius = 2
        marker.setAccessibilityElement(true); marker.setAccessibilityLabel("透明角落测试红点")
        addSubview(marker)
        let help = NSTextField(labelWithString: "透明角落验收：点胶囊左上空角的小红点，背景点击应增加。")
        help.textColor = .black; help.frame = NSRect(x: 20, y: 110, width: 800, height: 24); addSubview(help)
        markerTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.positionMarker() }
    }
    func positionMarker() {
        guard let window, let data = try? Data(contentsOf: URL(fileURLWithPath: "/tmp/agentwatch-material-probe.json")),
              let record = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let x = record["x"] as? Double, let y = record["y"] as? Double, let h = record["height"] as? Double else { return }
        let local = window.convertPoint(fromScreen: NSPoint(x: x + 3, y: y + h - 3))
        marker.setFrameOrigin(NSPoint(x: local.x - 2, y: local.y - 2))
    }
    func backgroundClick() { count += 1; counter.stringValue = "背景点击 \(count)" }

    required init?(coder: NSCoder) { fatalError() }
    override func mouseDown(with event: NSEvent) { backgroundClick() }
}
final class Host: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var dark = false
    var host: HostView!
    func applicationDidFinishLaunching(_ n: Notification) {
        window = NSWindow(contentRect: NSRect(x: 100, y: 160, width: 900, height: 600), styleMask: [.titled,.resizable,.closable,.miniaturizable], backing: .buffered, defer: false)
        window.title = "胶囊验收文档 · 人工内容"
        host = HostView(frame: NSRect(x: 0, y: 0, width: 900, height: 600))
        host.autoresizingMask = [.width,.height]
        window.contentView = host
        let text = NSTextView(frame: NSRect(x: 30, y: 200, width: 800, height: 300))
        text.string = "连续输入测试："
        text.font = .systemFont(ofSize: 24)
        text.isRichText = false
        text.autoresizingMask = [.width]
        text.setAccessibilityLabel("验收输入框")
        host.addSubview(text)
        let button = NSButton(title: "切换浅深背景", target: self, action: #selector(theme))
        button.frame = NSRect(x: 30, y: 70, width: 200, height: 36)
        host.addSubview(button)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(text)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc func theme() { dark.toggle(); host.layer?.backgroundColor = (dark ? NSColor(calibratedRed: 0.06, green: 0.11, blue: 0.18, alpha: 1) : .white).cgColor }
}
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let host = Host(); app.delegate = host; app.run()
