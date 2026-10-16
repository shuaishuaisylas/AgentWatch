import AppKit

/// Neither the capsule nor its details ever becomes a keyboard destination.
class PassivePanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
    init(size: NSSize) {
        super.init(contentRect: NSRect(origin: .zero, size: size),
                   styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isOpaque = false; backgroundColor = .clear; hasShadow = false
        level = .floating; hidesOnDeactivate = false; isFloatingPanel = true
        becomesKeyOnlyIfNeeded = true
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        isReleasedWhenClosed = false; animationBehavior = .none
        acceptsMouseMovedEvents = true
        title = "AgentWatch 无痛胶囊"
        setAccessibilityTitle(title)
    }
}

/// Direct window content effect: do not flatten the backdrop into a clipped parent layer.
final class CapsuleView: NSVisualEffectView {
    let edges = NSView()
    let text = NSTextField(labelWithString: "")
    let track = CAShapeLayer(), used = CAShapeLayer(), shine = CAShapeLayer()
    var onClick: (() -> Void)?
    var onDragEnd: (() -> Void)?
    var isDragging = false, isPressed = false
    var trace = "none"
    private var downPoint = NSPoint.zero, downOrigin = NSPoint.zero
    private var lastAnimationFraction: Double?
    private struct RenderInputs: Equatable {
        let message: String; let active: Int?; let pending: Int?; let demo: Bool
        let fraction: Double?; let stale: Bool; let animate: Bool
    }
    private var lastRender: RenderInputs?
    override var acceptsFirstResponder: Bool { false }
    override var needsPanelToBecomeKey: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override init(frame: NSRect) {
        super.init(frame: frame)
        blendingMode = .behindWindow
        material = .hudWindow
        state = .active
        edges.wantsLayer = true
        addSubview(edges)
        text.font = .systemFont(ofSize: 13, weight: .medium)
        text.textColor = .labelColor; text.alignment = .center; text.isSelectable = false
        addSubview(text)
        for line in [track, used, shine] {
            line.fillColor = nil; line.lineWidth = 1.1; line.lineCap = .butt
            edges.layer?.addSublayer(line)
        }
        track.strokeColor = NSColor.labelColor.withAlphaComponent(0.18).cgColor
        used.strokeColor = NSColor.systemTeal.withAlphaComponent(0.85).cgColor
        used.strokeEnd = 0.38
        shine.strokeColor = NSColor.white.withAlphaComponent(0.7).cgColor
        shine.lineWidth = 1.3; shine.isHidden = true
        setAccessibilityElement(true); setAccessibilityRole(.button)
        setAccessibilityLabel("")
    }
    required init?(coder: NSCoder) { fatalError() }
    override func layout() {
        super.layout()
        edges.frame = bounds
        text.frame = NSRect(x: 8, y: (bounds.height - 18) / 2, width: bounds.width - 16, height: 18)
        let mask = NSImage(size: bounds.size)
        mask.lockFocus()
        NSColor.black.withAlphaComponent(NSWorkspace.shared.accessibilityDisplayShouldReduceTransparency || NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast ? 1 : 0.42).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: bounds.height / 2, yRadius: bounds.height / 2).fill()
        mask.unlockFocus()
        maskImage = mask
        let rect = bounds.insetBy(dx: 0.7, dy: 0.7), r = (bounds.height - 1.4) / 2
        let p = CGMutablePath()
        p.move(to: CGPoint(x: rect.midX, y: rect.maxY))
        p.addLine(to: CGPoint(x: rect.maxX - r, y: rect.maxY))
        p.addArc(center: CGPoint(x: rect.maxX - r, y: rect.midY), radius: r, startAngle: .pi / 2, endAngle: -.pi / 2, clockwise: true)
        p.addLine(to: CGPoint(x: rect.minX + r, y: rect.minY))
        p.addArc(center: CGPoint(x: rect.minX + r, y: rect.midY), radius: r, startAngle: -.pi / 2, endAngle: -.pi * 1.5, clockwise: true)
        p.addLine(to: CGPoint(x: rect.midX, y: rect.maxY)); p.closeSubpath()
        CATransaction.begin(); CATransaction.setDisableActions(true)
        for line in [track, used, shine] { line.frame = bounds; line.path = p }
        CATransaction.commit()
    }
    func render(message: String, active: Int?, pending: Int?, demo: Bool, fraction: Double?, stale: Bool, animate: Bool) {
        let inputs = RenderInputs(message: message, active: active, pending: pending, demo: demo, fraction: fraction, stale: stale, animate: animate)
        guard inputs != lastRender || (animate && shine.animation(forKey: "shine") == nil) else { return }
        lastRender = inputs
        let value = message.isEmpty ? "" : (demo ? "演示 · " : "") + message
        let string = NSMutableAttributedString(string: value, attributes: [.font: NSFont.systemFont(ofSize: 13, weight: .medium), .foregroundColor: NSColor.labelColor])
        for (number, suffix, color) in [(active, " 执行", NSColor.systemTeal), (pending, " 待处理", NSColor.systemOrange)] {
            if let number, let range = value.range(of: "\(number)" + suffix) {
                let start = NSRange(range, in: value).location
                string.addAttributes([.font: NSFont.monospacedDigitSystemFont(ofSize: 14, weight: .semibold), .foregroundColor: number > 0 ? color : .labelColor], range: NSRange(location: start, length: String(number).count))
            }
        }
        text.attributedStringValue = string
        setAccessibilityLabel(value.isEmpty ? "" : value + "，点击查看详情，可拖动" + (fraction == nil ? "，额度未知" : stale ? "，额度旧快照" : ""))
        let targetWidth = max(demo ? 236 : 212, string.size().width + 36)
        if abs(bounds.width - targetWidth) > 0.5, let window {
            window.setContentSize(NSSize(width: ceil(targetWidth), height: 38))
        }
        CATransaction.begin(); CATransaction.setDisableActions(true)
        track.lineDashPattern = fraction == nil ? [2, 4] : nil
        used.strokeEnd = CGFloat(fraction ?? 0)
        used.opacity = stale ? 0.35 : 0.85
        used.lineDashPattern = stale ? [2, 3] : nil
        CATransaction.commit()
        if animate, let fraction, fraction > 0 {
            if lastAnimationFraction != fraction || shine.animation(forKey: "shine") == nil {
                let width = min(0.035, fraction * 0.35)
                let start = CABasicAnimation(keyPath: "strokeStart"); start.fromValue = 0; start.toValue = fraction - width
                let end = CABasicAnimation(keyPath: "strokeEnd"); end.fromValue = width; end.toValue = fraction
                let group = CAAnimationGroup(); group.animations = [start, end]; group.duration = 5; group.repeatCount = .infinity
                group.timingFunction = CAMediaTimingFunction(name: .linear)
                shine.isHidden = false; shine.add(group, forKey: "shine"); lastAnimationFraction = fraction
            }
        } else { stopAnimation() }
    }
    func stopAnimation() { shine.removeAnimation(forKey: "shine"); shine.isHidden = true; lastAnimationFraction = nil }
    override func hitTest(_ point: NSPoint) -> NSView? {
        let local = convert(point, from: superview)
        return NSBezierPath(roundedRect: bounds, xRadius: bounds.height / 2, yRadius: bounds.height / 2).contains(local) ? self : nil
    }
    private func screenPoint(for event: NSEvent) -> NSPoint {
        // Preserve the event's global position even if queued events were created
        // before a previous drag update moved this window.
        if let point = event.cgEvent?.location, let top = NSScreen.screens.first?.frame.maxY {
            return NSPoint(x: point.x, y: top - point.y)
        }
        return window?.convertPoint(toScreen: event.locationInWindow) ?? .zero
    }
    override func mouseDown(with event: NSEvent) {
        trace = "down: \(event.locationInWindow)"; isPressed = true
        downPoint = screenPoint(for: event)
        downOrigin = window?.frame.origin ?? .zero; isDragging = false
    }
    override func mouseDragged(with event: NSEvent) {
        guard isPressed else { return }
        let mouse = screenPoint(for: event)
        let delta = NSPoint(x: mouse.x - downPoint.x, y: mouse.y - downPoint.y)
        trace = "drag: \(event.locationInWindow)"
        if hypot(delta.x, delta.y) > 4 { isDragging = true }
        if isDragging { window?.setFrameOrigin(NSPoint(x: downOrigin.x + delta.x, y: downOrigin.y + delta.y)) }
    }
    override func mouseUp(with event: NSEvent) {
        guard isPressed else { return }
        trace = "up: \(event.locationInWindow) dragged=\(isDragging)"; isPressed = false
        if isDragging { onDragEnd?() } else { onClick?() }; isDragging = false
    }
    override func accessibilityPerformPress() -> Bool { onClick?(); return true }
}
