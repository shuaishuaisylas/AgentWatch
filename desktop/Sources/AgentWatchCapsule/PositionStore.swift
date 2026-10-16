import AppKit

final class PositionStore {
    let preferences: UserDefaults
    init(preferences: UserDefaults) { self.preferences = preferences }
    func bestScreen(for frame: NSRect) -> NSScreen? {
        NSScreen.screens.max { a, b in
            let ar = a.visibleFrame.intersection(frame), br = b.visibleFrame.intersection(frame)
            return max(0, ar.width) * max(0, ar.height) < max(0, br.width) * max(0, br.height)
        }
    }
    func restore(_ panel: NSPanel) {
        let saved = preferences.dictionary(forKey: "position")
        let screen = NSScreen.screens.first { id($0) == saved?["screen"] as? String } ?? NSScreen.main ?? NSScreen.screens.first
        guard let screen else { return }
        let available = screen.visibleFrame.insetBy(dx: 8, dy: 8)
        let x = saved?["x"] as? Double ?? 0.97
        let y = saved?["y"] as? Double ?? 0.95
        panel.setFrameOrigin(NSPoint(x: available.minX + max(0, available.width - panel.frame.width) * min(1, max(0, x)),
                                     y: available.minY + max(0, available.height - panel.frame.height) * min(1, max(0, y))))
        clamp(panel, snap: false)
    }
    func reset(_ panel: NSPanel) { preferences.removeObject(forKey: "position"); restore(panel); save(panel) }
    func clamp(_ panel: NSPanel, snap: Bool) {
        guard let screen = bestScreen(for: panel.frame) else { return }
        let area = screen.visibleFrame.insetBy(dx: 8, dy: 8)
        var p = panel.frame.origin
        p.x = min(max(p.x, area.minX), max(area.minX, area.maxX - panel.frame.width))
        p.y = min(max(p.y, area.minY), max(area.minY, area.maxY - panel.frame.height))
        if snap {
            if abs(p.x - area.minX) < 16 { p.x = area.minX }
            if abs(p.x + panel.frame.width - area.maxX) < 16 { p.x = area.maxX - panel.frame.width }
            if abs(p.y - area.minY) < 16 { p.y = area.minY }
            if abs(p.y + panel.frame.height - area.maxY) < 16 { p.y = area.maxY - panel.frame.height }
        }
        panel.setFrameOrigin(p)
    }
    func save(_ panel: NSPanel) {
        guard let screen = bestScreen(for: panel.frame) else { return }
        let area = screen.visibleFrame.insetBy(dx: 8, dy: 8)
        preferences.set(["screen": id(screen), "x": (panel.frame.minX - area.minX) / max(1, area.width - panel.frame.width),
                         "y": (panel.frame.minY - area.minY) / max(1, area.height - panel.frame.height)], forKey: "position")
    }
    private func id(_ screen: NSScreen) -> String { (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.stringValue ?? "unknown" }
}
