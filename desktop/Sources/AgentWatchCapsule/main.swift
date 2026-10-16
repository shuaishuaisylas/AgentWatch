import AppKit
import CapsuleCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let panel = PassivePanel(size: NSSize(width: 236, height: 38))
    let capsule = CapsuleView(frame: NSRect(x: 0, y: 0, width: 236, height: 38))
    let details = DetailsPanel()
    let controller: ClientController
    let preferences: UserDefaults
    let positions: PositionStore
    var quotaSelection: QuotaSelection
    var statusItem: NSStatusItem!
    var timer: Timer?
    var passthroughTimer: Timer?
    var globalMonitor: Any?
    var localMonitor: Any?
    var observers: [NSObjectProtocol] = []
    private var quotaMenu: NSMenu?
    private var isTrackingMenu = false
    private var lastWorkingApplication = NSWorkspace.shared.frontmostApplication
    private var menuReturnApplication: NSRunningApplication?
    private let layoutProbe = ProcessInfo.processInfo.environment["AGENTWATCH_CAPSULE_QA_DETAILS"] == "1"
    private var lastDetailsSignature = ""

    override init() {
        let args = ProcessInfo.processInfo.arguments
        let probe = args.contains("--material-probe") || Bundle.main.object(forInfoDictionaryKey: "AWMaterialProbe") as? Bool == true
        let demo = args.contains("--expect-demo") || probe
        var address = "http://127.0.0.1:8766"
        if let flag = args.firstIndex(of: "--url"), flag + 1 < args.count { address = args[flag + 1] }
        // Invalid URLs fail closed in APIClient; they never become a navigation destination.
        let url = URL(string: address) ?? URL(string: "http://127.0.0.1:0")!
        controller = ClientController(baseURL: url, expectDemo: demo, probe: probe)
        // The real app's bundle domain is already UserDefaults.standard. Opening
        // that same identifier as a suite returns nil on current macOS.
        if probe { preferences = UserDefaults(suiteName: "local.agentwatch.capsule.probe")! }
        else if demo { preferences = UserDefaults(suiteName: "local.agentwatch.capsule.demo")! }
        else { preferences = .standard }
        positions = PositionStore(preferences: preferences)
        let identity = preferences.data(forKey: "quotaIdentity").flatMap { try? JSONDecoder().decode(QuotaIdentity.self, from: $0) }
        quotaSelection = QuotaSelection(identity: identity, automaticallySelectWeekly: !preferences.bool(forKey: "quotaSelectionMade"))
        super.init()
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        // A second process cannot silently replace the first process's origin or credentials.
        let peers = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "local.agentwatch.capsule")
        if peers.contains(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
            fputs("胶囊已运行，请先从菜单栏退出，再选择其他服务。\n", stderr)
            NSApp.terminate(nil); return
        }
        panel.contentView = capsule
        positions.restore(panel)
        capsule.onClick = { [weak self] in self?.toggleDetails() }
        capsule.onDragEnd = { [weak self] in
            guard let self else { return }
            self.closeDetails(); self.positions.clamp(self.panel, snap: true); self.positions.save(self.panel)
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "胶囊"
        let menu = NSMenu()
        menu.delegate = self
        for (title, selector) in [("显示胶囊", #selector(show)), ("暂隐胶囊", #selector(hide)), ("重置位置", #selector(reset)), ("打开完整看板", #selector(openDashboard)), ("刷新数据", #selector(refresh)), ("退出胶囊", #selector(quit))] {
            let item = menu.addItem(withTitle: title, action: selector, keyEquivalent: ""); item.target = self
        }
        let quotaItem = NSMenuItem(title: "额度来源", action: nil, keyEquivalent: "")
        quotaItem.submenu = makeQuotaMenu(); menu.insertItem(quotaItem, at: 3)
        statusItem.menu = menu
        let mouseEvents: NSEvent.EventTypeMask = [.mouseMoved, .leftMouseDown, .rightMouseDown, .otherMouseDown, .leftMouseUp, .rightMouseUp, .otherMouseUp]
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: mouseEvents) { [weak self] event in
            guard let self else { return }
            self.updatePassthrough()
            if [.leftMouseDown, .rightMouseDown, .otherMouseDown].contains(event.type), !self.layoutProbe && !self.isTrackingMenu { self.closeDetails() }
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: mouseEvents) { [weak self] event in
            guard let self else { return event }
            self.updatePassthrough()
            if [.leftMouseDown, .rightMouseDown, .otherMouseDown].contains(event.type), !self.layoutProbe && !self.isTrackingMenu && event.window !== self.panel && event.window !== self.details { self.closeDetails() }
            return event // The working click always reaches its original destination.
        }
        let hitTimer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in MainActor.assumeIsolated { self?.updatePassthrough() } }
        RunLoop.main.add(hitTimer, forMode: .common); passthroughTimer = hitTimer
        let workspace = NSWorkspace.shared.notificationCenter
        observers.append(workspace.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in MainActor.assumeIsolated { self?.closeDetails(); self?.controller.sleep() } })
        observers.append(workspace.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in MainActor.assumeIsolated { self?.controller.wake() } })
        observers.append(workspace.addObserver(forName: NSWorkspace.screensDidSleepNotification, object: nil, queue: .main) { [weak self] _ in MainActor.assumeIsolated { self?.controller.sleep() } })
        observers.append(workspace.addObserver(forName: NSWorkspace.screensDidWakeNotification, object: nil, queue: .main) { [weak self] _ in MainActor.assumeIsolated { self?.controller.wake() } })
        observers.append(workspace.addObserver(forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main) { [weak self] _ in MainActor.assumeIsolated { if self?.layoutProbe != true { self?.closeDetails() } } })
        observers.append(workspace.addObserver(forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.capsule.needsLayout = true; self?.render() }
        })
        observers.append(NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                if !self.layoutProbe { self.closeDetails() }
                self.positions.restore(self.panel)
                if self.layoutProbe { self.details.place(nextTo: self.panel) }
            }
        })
        controller.onChange = { [weak self] in self?.render() }
        let hidden = !controller.probe && preferences.bool(forKey: "hidden")
        controller.hidden = hidden
        let poll = Timer(timeInterval: 1, repeats: true) { [weak self] _ in MainActor.assumeIsolated { self?.controller.tick() } }
        RunLoop.main.add(poll, forMode: .common); timer = poll
        controller.tick(force: true)
        render()
        if ProcessInfo.processInfo.environment["AGENTWATCH_CAPSULE_QA_DETAILS"] == "1" { toggleDetails(); panel.orderOut(nil) }
    }
    func applicationWillTerminate(_ notification: Notification) {
        capsule.stopAnimation(); timer?.invalidate(); passthroughTimer?.invalidate(); controller.stop()
        if let globalMonitor { NSEvent.removeMonitor(globalMonitor) }
        if let localMonitor { NSEvent.removeMonitor(localMonitor) }
        for observer in observers { NotificationCenter.default.removeObserver(observer); NSWorkspace.shared.notificationCenter.removeObserver(observer) }
    }
    func render() {
        if let front = NSWorkspace.shared.frontmostApplication, front.processIdentifier != ProcessInfo.processInfo.processIdentifier { lastWorkingApplication = front }
        let previous = quotaSelection.identity
        quotaSelection.resolve(snapshot: controller.subscriptions, now: Date().timeIntervalSince1970)
        if previous != quotaSelection.identity { saveQuota() }
        let status = controller.status
        let shouldShow = !controller.hidden && !layoutProbe
        if !shouldShow {
            panel.orderOut(nil)
            capsule.isPressed = false; capsule.isDragging = false
            if !layoutProbe { closeDetails() }
        }
        let quota = QuotaPresentation.evaluate(snapshot: controller.subscriptions, selection: quotaSelection.identity, failed: controller.subscriptionError != nil, now: Date().timeIntervalSince1970)
        let animate = status.fresh && (status.active ?? 0) > 0 && quota.freshness == .valid && shouldShow && !controller.suspended && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let oldSize = panel.frame.size
        capsule.render(message: status.text, active: status.active, pending: status.pending, demo: controller.expectDemo, fraction: quota.fraction, stale: quota.freshness == .stale, animate: animate)
        if oldSize != panel.frame.size && !capsule.isPressed { positions.clamp(panel, snap: false) }
        if shouldShow && !panel.isVisible { panel.orderFrontRegardless() }
        if details.isVisible && !isTrackingMenu {
            let signature = String(describing: controller.snapshot) + String(describing: controller.children) + String(describing: controller.subscriptions) + (controller.snapshotError ?? "") + (controller.subscriptionError ?? "") + String(controller.actionBusy) + (controller.childrenError ?? "") + (controller.actionMessage ?? "") + quota.description + status.text + String(status.fresh) + (controller.selectedParent ?? "")
            if signature != lastDetailsSignature { lastDetailsSignature = signature; renderDetails(quota: quota) }
        }
        updatePassthrough()
        if controller.probe || ProcessInfo.processInfo.environment["AGENTWATCH_CAPSULE_QA"] == "1" { writeProbeRecord() }
    }
    func updatePassthrough() {
        guard panel.isVisible || details.isVisible else { return }
        guard !capsule.isPressed, NSEvent.pressedMouseButtons == 0 else { return }
        let cursor = NSEvent.mouseLocation
        for (window, radius) in [(panel, CGFloat(19)), (details, CGFloat(16))] {
            let p = window.convertPoint(fromScreen: cursor)
            let area = NSRect(origin: .zero, size: window.frame.size)
            let inside = window.isVisible && NSBezierPath(roundedRect: area, xRadius: radius, yRadius: radius).contains(p)
            if window.ignoresMouseEvents == inside { window.ignoresMouseEvents = !inside }
        }
    }
    func toggleDetails() {
        if details.isVisible { closeDetails(); return }
        guard layoutProbe || !controller.hidden else { return }
        controller.setDetailsVisible(true)
        lastDetailsSignature = ""
        let quota = QuotaPresentation.evaluate(snapshot: controller.subscriptions, selection: quotaSelection.identity, failed: controller.subscriptionError != nil, now: Date().timeIntervalSince1970)
        renderDetails(quota: quota)
        details.place(nextTo: panel); details.orderFrontRegardless()
    }
    func closeDetails() { details.orderOut(nil); controller.setDetailsVisible(false) }
    func renderDetails(quota: QuotaPresentation) {
        let offset = details.beginUpdate()
        details.text(controller.expectDemo ? "人工演示 · 会话" : "会话", title: true)
        if controller.snapshot == nil { details.text(controller.snapshotError == nil ? "正在连接…" : "状态待更新", warning: true) }
        else if !controller.status.fresh { details.text("状态待更新", warning: true) }
        else if controller.status.hasCoverageGap { details.text("部分状态待确认", warning: true) }
        let groups = CapsuleDetailGroups.evaluate(snapshot: controller.snapshot, now: Date().timeIntervalSince1970)
        renderGroup("待回复 / 待批准", cards: groups.pending, completed: false,
                    empty: controller.status.pending == nil ? "待处理状态待确认" : "暂无待处理")
        renderGroup("正在执行", cards: groups.active, completed: false,
                    empty: controller.snapshot == nil ? "正在连接…" : "暂无其他执行中的会话")
        renderGroup("完成待查看", cards: groups.completed, completed: true,
                    empty: controller.snapshot?.inboxCounts == nil ? "完成记录待更新" : "暂无未读完成结果")
        if controller.snapshot?.truncated == true { details.text("更多会话见完整看板") }
        if let message = controller.actionMessage { details.text(message, warning: true) }
        details.endUpdate(offset: offset)
    }
    func renderGroup(_ title: String, cards: [SessionCard], completed: Bool, empty: String) {
        details.separator(); details.text(title, title: true)
        if cards.isEmpty { details.text(empty); return }
        for card in cards {
            var states: [String] = []
            if completed { states.append("本轮结束 · 待查看") }
            else {
                if (card.attention?.waitingUser ?? 0) > 0 { states.append("待回复") }
                if (card.attention?.waitingApproval ?? 0) > 0 { states.append("待批准") }
                if card.confirmedActive { states.append("执行中") }
                if states.isEmpty { states.append(card.stateLabel) }
            }
            if card.stale, !completed { states.append("较久未更新") }
            let target = card.jump
            if let target, target.available {
                if target.precision == "应用" { states.append("仅打开应用") }
                else if target.precision == "项目" { states.append("仅打开项目") }
                else if target.precision == "原标签页" { states.append("原标签页") }
            } else if !controller.expectDemo { states.append("暂不能定位") }
            let at = completed ? card.lastCompletedAt : (card.attention?.pending == true ? card.attention?.oldestAt ?? card.stateAt : card.stateAt)
            let summary = ([toolName(card.tool)] + states + [relativeTime(at)]).joined(separator: " · ")
            var readVersion: String?
            var readAction: (() -> Void)?
            var dismissVersion: String?
            var dismissAction: (() -> Void)?
            if completed, card.role == "main", card.unreadCompleted == true,
               let at = card.inbox?.completedAt, at.isFinite, at > 0, !controller.probe {
                readVersion = card.id + ":" + String(at)
                if !controller.actionBusy {
                    readAction = { [weak self] in self?.controller.markCompletionRead(id: card.id, through: at) }
                }
            }
            if !completed, card.requiresHumanAttention == true, let version = card.attentionDismissVersion, !controller.probe {
                dismissVersion = card.id + ":" + version
                if !controller.actionBusy {
                    dismissAction = { [weak self] in self?.controller.confirmNoPending(card) }
                }
            }
            details.session(name: card.displayName, summary: summary,
                            warning: card.attention?.pending == true,
                            enabled: target?.available == true && !controller.expectDemo && !controller.actionBusy,
                            identifier: "session:" + card.id,
                            readVersion: readVersion, readAction: readAction,
                            dismissVersion: dismissVersion, dismissAction: dismissAction) { [weak self] in self?.controller.performJump(card) }
        }
    }
    func relativeTime(_ time: TimeInterval?) -> String {
        guard let time, time.isFinite, time > 0 else { return "时间未知" }
        let elapsed = max(0, Date().timeIntervalSince1970 - time)
        if elapsed < 60 { return "刚刚" }
        if elapsed < 3600 { return "\(Int(elapsed / 60)) 分钟前" }
        if elapsed < 86400 { return "\(Int(elapsed / 3600)) 小时前" }
        return "\(Int(elapsed / 86400)) 天前"
    }
    func makeQuotaMenu() -> NSMenu {
        let menu = NSMenu()
        let presentation = QuotaPresentation.evaluate(snapshot: controller.subscriptions, selection: quotaSelection.identity,
                                                      failed: controller.subscriptionError != nil, now: Date().timeIntervalSince1970)
        let description = NSMenuItem(title: presentation.description, action: nil, keyEquivalent: "")
        description.isEnabled = false; menu.addItem(description); menu.addItem(.separator())
        let none = NSMenuItem(title: "不绑定额度", action: #selector(selectQuota(_:)), keyEquivalent: "")
        none.target = self; none.state = quotaSelection.identity == nil ? .on : .off; menu.addItem(none)
        for platform in controller.subscriptions?.platforms ?? [] {
            for limit in platform.limits {
                let identity = QuotaIdentity(tool: platform.tool, limit: limit)
                let item = NSMenuItem(title: "\(platform.name) · \(limit.label) · \(limit.bucket) · \(limit.status)", action: #selector(selectQuota(_:)), keyEquivalent: "")
                item.target = self; item.representedObject = QuotaChoice(identity)
                item.state = identity == quotaSelection.identity ? .on : .off; menu.addItem(item)
            }
        }
        return menu
    }
    func menuWillOpen(_ menu: NSMenu) {
        menuReturnApplication = NSWorkspace.shared.frontmostApplication.flatMap {
            $0.processIdentifier == ProcessInfo.processInfo.processIdentifier ? lastWorkingApplication : $0
        }
        if menu === statusItem?.menu {
            menu.items.first(where: { $0.title == "额度来源" })?.submenu = makeQuotaMenu()
        }
        isTrackingMenu = true
    }
    func menuDidClose(_ menu: NSMenu) {
        isTrackingMenu = false
        let workingApplication = menuReturnApplication
        menuReturnApplication = nil
        // AppKit can finish activating a context-menu owner after menuDidClose.
        // Restore on the next turn, only while this app still owns the foreground.
        DispatchQueue.main.async {
            if NSApp.isActive,
               NSWorkspace.shared.frontmostApplication?.processIdentifier == ProcessInfo.processInfo.processIdentifier,
               let workingApplication, workingApplication.processIdentifier != ProcessInfo.processInfo.processIdentifier {
                if #available(macOS 14.0, *) {
                    NSApp.yieldActivation(to: workingApplication)
                    workingApplication.activate(from: .current, options: [])
                } else { workingApplication.activate(options: []) }
            }
        }
    }
    @objc func selectQuota(_ item: NSMenuItem) { quotaSelection.select((item.representedObject as? QuotaChoice)?.identity); saveQuota(); lastDetailsSignature = ""; render() }
    func saveQuota() {
        preferences.set(true, forKey: "quotaSelectionMade")
        if let identity = quotaSelection.identity, let data = try? JSONEncoder().encode(identity) { preferences.set(data, forKey: "quotaIdentity") }
        else { preferences.removeObject(forKey: "quotaIdentity") }
    }
    func formatDate(_ time: TimeInterval?) -> String {
        guard let time, time.isFinite else { return "—" }
        return Date(timeIntervalSince1970: time).formatted(date: .abbreviated, time: .standard)
    }
    func toolName(_ tool: String) -> String { ["codex": "Codex", "claude": "Claude Code", "workbuddy": "WorkBuddy", "cursor": "Cursor"][tool] ?? tool }
    func sourceName(_ source: String?) -> String { guard let source else { return "来源未知" }; return ["hook": "状态上报", "event": "明确事件", "record": "本地记录", "process": "进程观测", "demo": "人工样例"][source] ?? source }
    @objc func show() { controller.hidden = false; preferences.set(false, forKey: "hidden"); controller.tick(force: true); render() }
    @objc func hide() { closeDetails(); panel.orderOut(nil); capsule.isPressed = false; capsule.isDragging = false; capsule.stopAnimation(); controller.hidden = true; preferences.set(true, forKey: "hidden") }
    @objc func reset() { positions.reset(panel); show() }
    @objc func openDashboard() {
        guard !controller.probe, controller.snapshot != nil else { return }
        NSWorkspace.shared.open(controller.baseURL)
    }
    @objc func refresh() { controller.requestRefresh() }
    @objc func quit() { NSApp.terminate(nil) }
    func writeProbeRecord() {
        let row: [String: Any] = ["ignoresMouse": panel.ignoresMouseEvents, "shine": capsule.shine.animation(forKey: "shine") != nil, "stroke": Double(capsule.used.strokeEnd), "stackHeight": details.stack.frame.height, "stackFittingHeight": details.stack.fittingSize.height, "detailsKey": details.isKeyWindow, "capsuleVisible": panel.isVisible, "onActiveSpace": panel.isOnActiveSpace, "capsuleWindow": panel.windowNumber, "detailsWindow": details.windowNumber, "snapshotFresh": controller.status.fresh, "quota": quotaSelection.identity?.key ?? "none", "key": panel.isKeyWindow, "main": panel.isMainWindow, "active": NSApp.isActive, "details": details.isVisible, "trace": capsule.trace, "x": panel.frame.minX, "y": panel.frame.minY, "width": panel.frame.width, "height": panel.frame.height]
        if let data = try? JSONSerialization.data(withJSONObject: row, options: .sortedKeys) { try? data.write(to: URL(fileURLWithPath: "/tmp/agentwatch-material-probe.json")) }
    }
}
private final class QuotaChoice: NSObject { let identity: QuotaIdentity; init(_ identity: QuotaIdentity) { self.identity = identity } }
MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
}
