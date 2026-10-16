import AppKit

class ActionButton: NSButton {
    var invoke: (() -> Void)?
    var onSecondary: ((NSEvent) -> Void)?
    override func rightMouseDown(with event: NSEvent) {
        if let onSecondary { onSecondary(event) } else { super.rightMouseDown(with: event) }
    }
    convenience init(_ title: String, action: @escaping () -> Void) {
        self.init(title: title, target: nil, action: nil)
        target = self; self.action = #selector(invokeAction)
        invoke = action; bezelStyle = .rounded
        font = .systemFont(ofSize: 11)
    }
    @objc private func invokeAction() { invoke?() }
    override var needsPanelToBecomeKey: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

final class TopStackView: NSStackView { override var isFlipped: Bool { true } }

private final class RowLabel: NSTextField {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        if newSize.width > 0, abs(preferredMaxLayoutWidth - newSize.width) > 0.5 {
            preferredMaxLayoutWidth = newSize.width
            invalidateIntrinsicContentSize()
        }
    }
}

/// The entire wrapping card is the navigation target; it never becomes key.
final class SessionRow: NSView {
    let nameLabel: NSTextField = RowLabel(wrappingLabelWithString: "")
    let summaryLabel: NSTextField = RowLabel(wrappingLabelWithString: "")
    let navigationButton = ActionButton("", action: {})
    private var readVersion: String?
    private var dismissVersion: String?
    private var readAction: (() -> Void)?
    private var dismissAction: (() -> Void)?
    var invoke: (() -> Void)? {
        get { navigationButton.invoke }
        set { navigationButton.invoke = newValue }
    }
    var isEnabled: Bool {
        get { navigationButton.isEnabled }
        set { navigationButton.isEnabled = newValue }
    }
    var action: Selector? { navigationButton.action }
    var target: AnyObject? { navigationButton.target }
    func performClick(_ sender: Any?) { navigationButton.performClick(sender) }
    init() {
        super.init(frame: .zero)
        navigationButton.isBordered = false
        navigationButton.translatesAutoresizingMaskIntoConstraints = false
        navigationButton.onSecondary = { [weak self] _ in self?.performSecondaryAction() }
        addSubview(navigationButton)
        for label in [nameLabel, summaryLabel] {
            label.translatesAutoresizingMaskIntoConstraints = false
            label.maximumNumberOfLines = 0; label.lineBreakMode = .byWordWrapping
            label.setContentCompressionResistancePriority(.required, for: .vertical)
            label.isSelectable = false; label.setAccessibilityElement(false); addSubview(label)
        }
        nameLabel.font = .systemFont(ofSize: 13, weight: .medium)
        summaryLabel.font = .systemFont(ofSize: 11)
        summaryLabel.textColor = .secondaryLabelColor
        NSLayoutConstraint.activate([
            heightAnchor.constraint(greaterThanOrEqualToConstant: 48),
            navigationButton.leadingAnchor.constraint(equalTo: leadingAnchor),
            navigationButton.trailingAnchor.constraint(equalTo: trailingAnchor),
            navigationButton.topAnchor.constraint(equalTo: topAnchor),
            navigationButton.bottomAnchor.constraint(equalTo: bottomAnchor),
            nameLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            nameLabel.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6),
            nameLabel.topAnchor.constraint(equalTo: topAnchor, constant: 5),
            summaryLabel.leadingAnchor.constraint(equalTo: nameLabel.leadingAnchor),
            summaryLabel.trailingAnchor.constraint(equalTo: nameLabel.trailingAnchor),
            summaryLabel.topAnchor.constraint(equalTo: nameLabel.bottomAnchor, constant: 4),
            summaryLabel.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -5)
        ])
    }
    required init?(coder: NSCoder) { fatalError() }
    override var needsPanelToBecomeKey: Bool { false }
    override var acceptsFirstResponder: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    private func performSecondaryAction() {
        if dismissVersion != nil, let dismissAction { dismissAction() }
        else if readVersion != nil, let readAction { readAction() }
    }
    override func rightMouseDown(with event: NSEvent) { performSecondaryAction() }
    func configureRead(version: String?, action: (() -> Void)?) {
        readVersion = version?.isEmpty == false ? version : nil
        readAction = readVersion == nil ? nil : action
    }
    func configureDismiss(version: String?, action: (() -> Void)?) {
        dismissVersion = version?.isEmpty == false ? version : nil
        dismissAction = dismissVersion == nil ? nil : action
    }
}

/// An independent overlay, so the system's always-visible preference cannot
/// reserve a gutter or leave the detail scrollbar visible while idle.
private final class ActivityScroller: NSScroller {
    var onScroll: ((Double) -> Void)?
    var onTracking: ((Bool) -> Void)?
    override var isFlipped: Bool { true }
    override var needsPanelToBecomeKey: Bool { false }
    override var acceptsFirstResponder: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? {
        isHidden || !isEnabled ? nil : super.hitTest(point)
    }
    private var thumb: NSRect {
        let height = min(bounds.height, max(26, bounds.height * knobProportion))
        return NSRect(x: 1, y: (bounds.height - height) * CGFloat(doubleValue), width: max(0, bounds.width - 2), height: height)
    }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.labelColor.withAlphaComponent(0.38).setFill()
        NSBezierPath(roundedRect: thumb, xRadius: 3, yRadius: 3).fill()
    }
    override func mouseDown(with event: NSEvent) {
        guard !isHidden, isEnabled else { return }
        onTracking?(true)
        defer { onTracking?(false) }
        let point = convert(event.locationInWindow, from: nil)
        let offset = thumb.contains(point) ? point.y - thumb.minY : thumb.height / 2
        func move(_ event: NSEvent) {
            let y = convert(event.locationInWindow, from: nil).y
            let travel = max(1, bounds.height - thumb.height)
            doubleValue = Double(max(0, min(1, (y - offset) / travel)))
            needsDisplay = true; onScroll?(doubleValue)
        }
        move(event)
        while let next = NSApp.nextEvent(matching: [.leftMouseDragged, .leftMouseUp], until: .distantFuture,
                                         inMode: .eventTracking, dequeue: true) {
            if next.type == .leftMouseUp { break }
            move(next)
        }
    }
}

final class ActivityScrollView: NSScrollView {
    private let indicator = ActivityScroller(frame: .zero)
    private var hideTimer: Timer?
    private var liveScrolling = false
    private var momentumScrolling = false
    private var draggingIndicator = false
    var indicatorVisible: Bool { !indicator.isHidden }
    var indicatorView: NSScroller { indicator }
    var indicatorFrame: NSRect { indicator.frame }
    var indicatorFraction: Double { indicator.doubleValue }
    var indicatorProportion: CGFloat { indicator.knobProportion }
    override var needsPanelToBecomeKey: Bool { false }
    override var acceptsFirstResponder: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override init(frame: NSRect) {
        super.init(frame: frame)
        drawsBackground = false; borderType = .noBorder
        hasVerticalScroller = false; hasHorizontalScroller = false
        automaticallyAdjustsContentInsets = false
        contentInsets = NSEdgeInsets(top: 0, left: 0, bottom: 0, right: 0)
        contentView.postsBoundsChangedNotifications = true
        indicator.isHidden = true; indicator.scrollerStyle = .overlay
        addSubview(indicator, positioned: .above, relativeTo: nil)
        indicator.onScroll = { [weak self] fraction in
            guard let self, let documentView = self.documentView else { return }
            let maximum = max(0, documentView.frame.height - self.contentView.bounds.height)
            self.contentView.scroll(to: NSPoint(x: 0, y: maximum * CGFloat(fraction)))
            self.reflectScrolledClipView(self.contentView)
        }
        indicator.onTracking = { [weak self] tracking in
            self?.draggingIndicator = tracking; self?.showIndicator()
        }
        let center = NotificationCenter.default
        center.addObserver(self, selector: #selector(boundsChanged), name: NSView.boundsDidChangeNotification, object: contentView)
        center.addObserver(self, selector: #selector(liveScrollBegan), name: NSScrollView.willStartLiveScrollNotification, object: self)
        center.addObserver(self, selector: #selector(liveScrollChanged), name: NSScrollView.didLiveScrollNotification, object: self)
        center.addObserver(self, selector: #selector(liveScrollEnded), name: NSScrollView.didEndLiveScrollNotification, object: self)
    }
    required init?(coder: NSCoder) { fatalError() }
    deinit { hideTimer?.invalidate(); NotificationCenter.default.removeObserver(self) }
    override func layout() { super.layout(); updateIndicator() }
    override func reflectScrolledClipView(_ clipView: NSClipView) {
        super.reflectScrolledClipView(clipView); updateIndicator()
    }
    @objc private func boundsChanged() { updateIndicator() }
    @objc private func liveScrollBegan() { liveScrolling = true; showIndicator() }
    @objc private func liveScrollChanged() { showIndicator() }
    @objc private func liveScrollEnded() { liveScrolling = false; showIndicator() }
    override func scrollWheel(with event: NSEvent) {
        if event.momentumPhase.contains(.began) || event.momentumPhase.contains(.changed) { momentumScrolling = true }
        if event.momentumPhase.contains(.ended) || event.momentumPhase.contains(.cancelled) { momentumScrolling = false }
        showIndicator()
        super.scrollWheel(with: event)
        updateIndicator()
    }
    private func updateIndicator() {
        let height = contentView.bounds.height
        let documentHeight = documentView?.frame.height ?? height
        let maximum = max(0, documentHeight - height)
        indicator.frame = NSRect(x: bounds.maxX - 8, y: bounds.minY + 3, width: 7, height: max(0, bounds.height - 6))
        indicator.knobProportion = documentHeight > 0 ? min(1, height / documentHeight) : 1
        indicator.doubleValue = maximum > 0 ? Double(max(0, min(1, contentView.bounds.minY / maximum))) : 0
        indicator.isEnabled = maximum > 0
        if maximum <= 0 { indicator.isHidden = true }
        indicator.needsDisplay = true
    }
    private func showIndicator() {
        updateIndicator()
        guard indicator.isEnabled else { return }
        indicator.isHidden = false
        hideTimer?.invalidate()
        hideTimer = Timer.scheduledTimer(withTimeInterval: 0.45, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, !self.liveScrolling, !self.momentumScrolling, !self.draggingIndicator else { return }
                self.indicator.isHidden = true
            }
        }
    }
}

final class DetailsPanel: PassivePanel {
    let scroll = ActivityScrollView(frame: .zero)
    let stack = TopStackView()
    private var renderIndex = 0
    private let background = NSVisualEffectView()
    init() {
        super.init(size: NSSize(width: 360, height: 430))
        title = "胶囊轻详情"
        setAccessibilityTitle(title)
        background.material = .popover; background.blendingMode = .behindWindow; background.state = .active
        background.wantsLayer = true; background.layer?.cornerRadius = 16; background.layer?.masksToBounds = true
        contentView = background
        scroll.frame = NSRect(x: 12, y: 10, width: 336, height: 410)
        scroll.autoresizingMask = [.width, .height]
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 6
        stack.edgeInsets = NSEdgeInsets(top: 8, left: 5, bottom: 12, right: 5)
        stack.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = stack
        background.addSubview(scroll)
        NSLayoutConstraint.activate([stack.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor)])
    }
    func beginUpdate() -> CGFloat {
        let offset = scroll.contentView.bounds.origin.y
        renderIndex = 0
        return offset
    }
    func endUpdate(offset: CGFloat) {
        for view in stack.arrangedSubviews.dropFirst(renderIndex) { stack.removeArrangedSubview(view); view.removeFromSuperview() }
        contentView?.layoutSubtreeIfNeeded()
        stack.layoutSubtreeIfNeeded()
        scroll.contentView.scroll(to: NSPoint(x: 0, y: max(0, min(offset, stack.frame.height - scroll.contentView.bounds.height))))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    private func view<T: NSView>(_ type: T.Type, make: () -> T) -> T {
        defer { renderIndex += 1 }
        if renderIndex < stack.arrangedSubviews.count, let existing = stack.arrangedSubviews[renderIndex] as? T { return existing }
        if renderIndex < stack.arrangedSubviews.count {
            let old = stack.arrangedSubviews[renderIndex]; stack.removeArrangedSubview(old); old.removeFromSuperview()
        }
        let result = make()
        stack.insertArrangedSubview(result, at: renderIndex)
        if result is NSTextField || result is NSBox || result is SessionRow {
            result.translatesAutoresizingMaskIntoConstraints = false
            result.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -10).isActive = true
        }
        return result
    }
    func text(_ value: String, title: Bool = false, warning: Bool = false) {
        let label = view(NSTextField.self) {
            let field = NSTextField(wrappingLabelWithString: "")
            field.maximumNumberOfLines = 0; field.translatesAutoresizingMaskIntoConstraints = false
            return field
        }
        label.stringValue = value
        label.font = .systemFont(ofSize: title ? 13 : 11, weight: title ? .semibold : .regular)
        label.textColor = warning ? .systemOrange : (title ? .labelColor : .secondaryLabelColor)
    }
    func button(_ title: String, enabled: Bool = true, identifier: String? = nil, action: @escaping () -> Void) {
        let button = view(ActionButton.self) { ActionButton(title, action: action) }
        button.title = title; button.isEnabled = enabled; button.invoke = action
        button.setAccessibilityIdentifier(identifier)
    }
    func session(name: String, summary: String, warning: Bool, enabled: Bool,
                 identifier: String,
                 readVersion: String? = nil, readAction: (() -> Void)? = nil,
                 dismissVersion: String? = nil, dismissAction: (() -> Void)? = nil,
                 action: @escaping () -> Void) {
        let row = view(SessionRow.self) { SessionRow() }
        row.nameLabel.stringValue = name; row.summaryLabel.stringValue = summary
        row.nameLabel.textColor = warning ? .systemOrange : .labelColor
        row.isEnabled = enabled || readAction != nil || dismissAction != nil
        row.invoke = enabled ? action : nil
        row.configureRead(version: readVersion, action: readAction)
        row.configureDismiss(version: dismissVersion, action: dismissAction)
        row.setAccessibilityIdentifier(identifier)
        row.navigationButton.setAccessibilityIdentifier(identifier)
        row.setAccessibilityLabel(name + "，" + summary)
        row.navigationButton.setAccessibilityLabel(name + "，" + summary)
        row.toolTip = nil; row.navigationButton.toolTip = nil; row.nameLabel.toolTip = nil
        row.summaryLabel.toolTip = nil
    }
    func separator() {
        _ = view(NSBox.self) {
            let line = NSBox(); line.boxType = .separator
            return line
        }
    }
    func place(nextTo capsule: NSPanel) {
        guard let screen = NSScreen.screens.max(by: { $0.frame.intersection(capsule.frame).width < $1.frame.intersection(capsule.frame).width }) else { return }
        let area = screen.visibleFrame.insetBy(dx: 8, dy: 8)
        let h = min(430, area.height - 8)
        var y = capsule.frame.minY - h - 8
        if y < area.minY { y = capsule.frame.maxY + 8 }
        y = max(area.minY, min(y, area.maxY - h))
        let x = max(area.minX, min(capsule.frame.maxX - 360, area.maxX - 360))
        setFrame(NSRect(x: x, y: y, width: min(360, area.width), height: h), display: true)
    }
}
