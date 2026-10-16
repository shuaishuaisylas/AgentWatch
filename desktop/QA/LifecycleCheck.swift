import AppKit
import Foundation

/// WindowServer integration against the real UI classes, with artificial in-memory
/// probe data. This runner never creates a Python service or an API client.
@main
@MainActor
struct LifecycleCheck {
    private struct Failure: Error { let message: String }
    private static var checks = 0

    static func main() {
        let arguments = ProcessInfo.processInfo.arguments
        guard arguments.contains("--material-probe"), arguments.contains("--expect-demo"),
              let flag = arguments.firstIndex(of: "--url"), flag + 1 < arguments.count,
              arguments[flag + 1] == "http://127.0.0.1:1" else {
            fail("Runner requires its artificial probe arguments")
        }
        guard NSRunningApplication.runningApplications(withBundleIdentifier: "local.agentwatch.capsule").isEmpty else {
            fail("Exit the existing capsule before this window integration check")
        }

        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let suite = "local.agentwatch.capsule.probe"
        let preferences = UserDefaults(suiteName: suite)!
        let savedPreferences = preferences.persistentDomain(forName: suite)
        preferences.removePersistentDomain(forName: suite)
        defer {
            if let savedPreferences { preferences.setPersistentDomain(savedPreferences, forName: suite) }
            else { preferences.removePersistentDomain(forName: suite) }
        }

        let delegate = AppDelegate()
        app.delegate = delegate
        defer {
            delegate.applicationWillTerminate(Notification(name: NSApplication.willTerminateNotification))
            delegate.panel.orderOut(nil)
            delegate.details.orderOut(nil)
            if let statusItem = delegate.statusItem { NSStatusBar.system.removeStatusItem(statusItem) }
        }

        do {
            try require(delegate.controller.probe && delegate.controller.expectDemo, "Artificial probe mode is required")
            try require(delegate.controller.baseURL.absoluteString == "http://127.0.0.1:1", "Only the unused probe origin is permitted")
            delegate.applicationDidFinishLaunching(Notification(name: NSApplication.didFinishLaunchingNotification))
            settle()
            try require(delegate.panel.isVisible, "Capsule initially becomes visible")
            try require(!delegate.panel.isKeyWindow && !delegate.panel.isMainWindow, "Capsule remains non-key and non-main")
            try require(delegate.controller.status.fresh, "Artificial initial state is fresh")
            try require(delegate.controller.status.active == 3 && delegate.controller.status.pending == 3,
                        "Probe reports three executions, two human roots, and one unread completion")
            try require(delegate.capsule.shine.animation(forKey: "shine") != nil, "Confirmed artificial execution starts edge shine")

            delegate.menuWillOpen(delegate.statusItem.menu!)
            guard let quota = delegate.statusItem.menu?.items.first(where: { $0.title == "额度来源" })?.submenu,
                  let selected = quota.items.first(where: { $0.representedObject != nil }),
                  let choiceAction = selected.action,
                  let none = quota.items.first(where: { $0.title == "不绑定额度" }),
                  let noneAction = none.action else { throw Failure(message: "Quota source recovery menu is missing") }
            try require(NSApp.sendAction(choiceAction, to: selected.target, from: selected), "Quota choice dispatches from the menu")
            try require(delegate.quotaSelection.identity?.key == "artificial-week", "Quota menu selects the explicit window identity")
            try require(NSApp.sendAction(noneAction, to: none.target, from: none), "Unbind choice dispatches from the menu")
            try require(delegate.quotaSelection.identity == nil && !delegate.quotaSelection.automaticallySelectWeekly,
                        "Unbinding quota does not immediately default back")
            _ = NSApp.sendAction(choiceAction, to: selected.target, from: selected)
            delegate.menuDidClose(delegate.statusItem.menu!)

            delegate.toggleDetails()
            settle()
            try require(delegate.details.isVisible && !delegate.details.isKeyWindow && !delegate.details.isMainWindow,
                        "Explicit details stay non-key and non-main")
            let sectionTitles = delegate.details.stack.arrangedSubviews.compactMap { ($0 as? NSTextField)?.stringValue }
            try require(["待回复 / 待批准", "正在执行", "完成待查看"].allSatisfy(sectionTitles.contains),
                        "Details render the three explicit activity groups")
            let displayedRows = delegate.details.stack.arrangedSubviews.compactMap { $0 as? SessionRow }
            try require(displayedRows.count == 5, "Probe has five unique main-session cards across the three groups")
            let identifiers = displayedRows.compactMap { $0.accessibilityIdentifier() }
            try require(Set(identifiers).count == identifiers.count && identifiers.contains("session:codex:artificial-main"),
                        "A root that executes and needs a person appears once")
            try require(!delegate.details.stack.arrangedSubviews.contains { $0 is ActionButton },
                        "Session cards are the only detail buttons; no independent jump control is added")
            try require(displayedRows.allSatisfy { !$0.isEnabled }, "Artificial demo rows cannot invoke real navigation")
            delegate.toggleDetails()
            settle()
            try verifySessionCardCallbacks(nextTo: delegate.panel)
            try verifySessionNames(nextTo: delegate.panel)
            try verifyScrollVisibility(nextTo: delegate.panel)
            try verifyAutomaticVisibility(delegate)

            try menu("暂隐胶囊", action: #selector(AppDelegate.hide), delegate: delegate)
            settle()
            try require(!delegate.panel.isVisible && !delegate.details.isVisible, "Hide closes both windows")
            try require(delegate.controller.hidden, "Hide is reflected in controller state")
            try require(delegate.capsule.shine.animation(forKey: "shine") == nil, "Hidden capsule stops shine")

            // Simulate a new event through the ordinary controller/render path.
            // These counters and names are all deliberately artificial.
            delegate.controller.snapshot = Snapshot(demo: true, now: Date().timeIntervalSince1970,
                counts: Counts(active: 4, waiting: 2, error: 1, unknown: 0),
                childrenCounts: Counts(active: 1, waiting: 1, error: 0, unknown: 0, total: 2),
                sessions: [SessionCard(id: "codex:lifecycle-artificial", tool: "codex", state: "active",
                    role: "main", alias: "人工生命周期样例", stateSource: "event", liveness: "unknown",
                    attention: SessionAttention(pending: true, waitingUser: 1))],
                attentionCounts: AttentionCounts(pending: 2, waitingUser: 1, waitingApproval: 1), recentCompleted: [], inboxCounts: InboxCounts(pending: 2, actionRequired: 2, unreadCompleted: 0))
            delegate.controller.tick(force: true)
            settle()
            try require(delegate.controller.status.active == 4 && delegate.controller.status.pending == 2,
                        "Artificial update keeps child execution and error counts out of main execution and human attention")
            try require(!delegate.panel.isVisible && !delegate.details.isVisible, "New state never reverses a user's hide")
            try require(delegate.capsule.shine.animation(forKey: "shine") == nil, "Updates while hidden do not restart shine")

            try menu("显示胶囊", action: #selector(AppDelegate.show), delegate: delegate)
            settle()
            try require(delegate.panel.isVisible && !delegate.controller.hidden, "Menu action restores the capsule")
            try require(!delegate.panel.isKeyWindow && !delegate.panel.isMainWindow, "Restore does not make a key or main window")
            try require(delegate.controller.status.fresh, "Restore refreshes the artificial state")
            try require(delegate.capsule.shine.animation(forKey: "shine") != nil, "Visible refreshed execution can restart shine")

            // Reset uses the same menu selector as the shipped app. The capsule
            // alone is displaced; no other window or desktop setting is changed.
            delegate.panel.setFrameOrigin(NSPoint(x: -100_000, y: -100_000))
            try menu("重置位置", action: #selector(AppDelegate.reset), delegate: delegate)
            settle()
            try require(contained(delegate.panel.frame), "Reset returns the capsule to a usable screen")
            try require(delegate.panel.isVisible, "Reset preserves a visible recovery entry")

            delegate.positions.save(delegate.panel)
            let savedFrame = delegate.panel.frame
            delegate.panel.setFrameOrigin(NSPoint(x: -100_000, y: -100_000))
            delegate.positions.restore(delegate.panel)
            settle()
            try require(contained(delegate.panel.frame), "Saved position restoration is contained")
            try require(abs(savedFrame.minX - delegate.panel.frame.minX) < 1 && abs(savedFrame.minY - delegate.panel.frame.minY) < 1,
                        "Saved relative position is restored")

            delegate.toggleDetails()
            settle()
            try require(delegate.details.isVisible, "Explicit click path opens details")
            delegate.controller.actionBusy = true
            delegate.controller.actionMessage = "人工待完成操作"
            let workspace = NSWorkspace.shared.notificationCenter
            workspace.post(name: NSWorkspace.willSleepNotification, object: nil)
            workspace.post(name: NSWorkspace.screensDidSleepNotification, object: nil)
            settle()
            try require(delegate.controller.suspended, "Registered sleep observers suspend the controller")
            try require(!delegate.details.isVisible, "Sleep observer closes details")
            try require(!delegate.panel.isVisible, "Sleep hides the capsule instead of showing an update placeholder")
            try require(!delegate.controller.actionBusy, "Sleep releases an interrupted action lock")
            try require(delegate.controller.actionMessage?.contains("未确认") == true, "Interrupted action stays explicitly unconfirmed")
            try require(!delegate.controller.status.fresh, "Suspended state cannot be presented as fresh")
            try require(delegate.capsule.shine.animation(forKey: "shine") == nil, "Sleep stops edge shine")

            workspace.post(name: NSWorkspace.didWakeNotification, object: nil)
            workspace.post(name: NSWorkspace.screensDidWakeNotification, object: nil)
            settle()
            try require(!delegate.controller.suspended && delegate.controller.status.fresh, "Repeated wake observers recover fresh artificial state")
            try require(delegate.panel.isVisible && !delegate.details.isVisible, "Wake preserves visibility without opening details")
            try require(delegate.capsule.shine.animation(forKey: "shine") != nil, "Wake restarts shine only after the probe refresh")

            try menu("暂隐胶囊", action: #selector(AppDelegate.hide), delegate: delegate)
            workspace.post(name: NSWorkspace.willSleepNotification, object: nil)
            workspace.post(name: NSWorkspace.didWakeNotification, object: nil)
            settle()
            try require(!delegate.panel.isVisible && delegate.controller.hidden, "Sleep and wake respect a user's existing hide")
            try require(delegate.capsule.shine.animation(forKey: "shine") == nil, "Hidden wake keeps shine stopped")
            try menu("显示胶囊", action: #selector(AppDelegate.show), delegate: delegate)
            settle()
            try require(delegate.panel.isVisible && !delegate.panel.isKeyWindow && !delegate.panel.isMainWindow, "Recovery menu remains usable after hidden wake")

            print("Lifecycle integration passed: \(checks) checks; artificial probe only.")
            print("Verified automatic activity visibility, manual-hide priority, menu wiring, three detail groups, whole-card action reuse, saved position, shine, and simulated sleep/wake notifications.")
        } catch let error as Failure {
            // Throwing out of the do block still runs window and preference cleanup.
            fputs("Lifecycle integration failed: \(error.message)\n", stderr)
            exitAfterCleanup = true
        } catch {
            fputs("Lifecycle integration failed: unexpected local test error\n", stderr)
            exitAfterCleanup = true
        }
        if exitAfterCleanup {
            // main's defers run before the process exits when this error escapes.
            // Use a nonzero exit at process shutdown without skipping those defers.
            atexit { _exit(1) }
        }
    }

    private static var exitAfterCleanup = false
    private static func verifyAutomaticVisibility(_ delegate: AppDelegate) throws {
        // The probe's timer normally refreshes its synthetic timestamp. Pause it
        // while testing stale/error input so it cannot accidentally mask either.
        let timerDate = delegate.timer?.fireDate
        let previousSnapshot = delegate.controller.snapshot
        let previousHidden = delegate.controller.hidden
        let previousError = delegate.controller.snapshotError
        delegate.timer?.fireDate = .distantFuture
        defer {
            delegate.closeDetails()
            delegate.controller.snapshot = previousSnapshot
            delegate.controller.snapshotError = previousError
            delegate.controller.lastSuccess = ProcessInfo.processInfo.systemUptime
            delegate.controller.suspended = false
            delegate.controller.hidden = previousHidden
            delegate.preferences.set(previousHidden, forKey: "hidden")
            delegate.render()
            if let timerDate { delegate.timer?.fireDate = timerDate }
        }
        func fixture(active: Int = 0, pending: Int = 0, unknown: Int = 0,
                     inboxUnknown: Int = 0, childActive: Int = 0, errors: Int = 0,
                     includeInbox: Bool = true) -> Snapshot {
            Snapshot(demo: true, now: Date().timeIntervalSince1970,
                counts: Counts(active: active, waiting: errors, error: errors, unknown: unknown, unknownRoles: unknown),
                childrenCounts: Counts(active: childActive, waiting: 0, error: 0, unknown: unknown, total: childActive + unknown),
                sessions: [], attentionCounts: AttentionCounts(pending: pending, waitingUser: pending, unknown: inboxUnknown),
                recentCompleted: [], inboxCounts: includeInbox ? InboxCounts(pending: pending, actionRequired: pending, unknown: inboxUnknown) : nil)
        }
        func apply(_ snapshot: Snapshot?, error: String? = nil, age: TimeInterval = 0) {
            delegate.controller.snapshot = snapshot
            delegate.controller.snapshotError = error
            delegate.controller.lastSuccess = ProcessInfo.processInfo.systemUptime - age
            delegate.render()
            settle(seconds: 0.04)
        }
        func hiddenWithoutContent(_ message: String) throws {
            try require(!delegate.panel.isVisible && !delegate.details.isVisible, message)
            try require(delegate.controller.status.text.isEmpty, "No activity produces no status label")
            try require(!delegate.controller.status.hasContent, "No activity is not visible capsule content")
            try require(delegate.capsule.text.stringValue.isEmpty && (delegate.capsule.accessibilityLabel() ?? "").isEmpty,
                        "Empty content clears the actual label, demo prefix, and accessible label")
            try require(!delegate.capsule.isPressed && !delegate.capsule.isDragging,
                        "Automatic hiding resets unfinished pointer gestures")
            try require(delegate.capsule.shine.animation(forKey: "shine") == nil, "An automatically hidden capsule never animates")
        }

        delegate.capsule.isPressed = true; delegate.capsule.isDragging = true
        apply(fixture())
        try hiddenWithoutContent("Known zero counts hide both windows and the quota outline")
        try require(delegate.capsule.used.strokeEnd > 0,
                    "A valid selected quota cannot keep an empty capsule shell visible")
        try require(delegate.controller.status.active == 0 && delegate.controller.status.pending == 0,
                    "Hiding known zero does not change the count semantics")
        try require(!delegate.controller.hidden && !delegate.preferences.bool(forKey: "hidden"),
                    "Automatic hiding never creates a persistent user hide")

        apply(fixture(unknown: 3, inboxUnknown: 2))
        try hiddenWithoutContent("Unknown-only state does not display a confirmation placeholder")
        try require(delegate.controller.status.pending == nil && delegate.controller.status.hasCoverageGap,
                    "Hidden coverage gaps remain unknown rather than becoming zero")
        delegate.toggleDetails()
        try require(!delegate.details.isVisible, "No-content capsule cannot open empty details")
        try menu("显示胶囊", action: #selector(AppDelegate.show), delegate: delegate)
        try hiddenWithoutContent("Show menu cannot force an unknown-only capsule into view")
        apply(fixture(includeInbox: false))
        try hiddenWithoutContent("Missing inbox coverage does not manufacture a visible zero or confirmation label")

        apply(fixture(active: 1, unknown: 2, inboxUnknown: 1))
        try require(delegate.panel.isVisible && delegate.controller.status.text == "1 执行",
                    "Confirmed execution automatically restores one positive label")
        try require(delegate.capsule.text.stringValue == "演示 · 1 执行" &&
                    !(delegate.capsule.accessibilityLabel() ?? "").contains("待确认"),
                    "Actual rendered content has only the positive activity label")
        try require(delegate.controller.status.pending == nil && delegate.controller.status.hasCoverageGap,
                    "Visible execution does not turn unknown pending into a zero label")
        try require(!delegate.details.isVisible && !delegate.panel.isKeyWindow && !delegate.panel.isMainWindow,
                    "Automatic reappearance neither opens details nor takes keyboard focus")
        guard let lateRelease = NSEvent.mouseEvent(with: .leftMouseUp, location: NSPoint(x: 20, y: 20),
            modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
            windowNumber: delegate.panel.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 0)
            else { throw Failure(message: "Cannot construct the artificial cancelled release") }
        delegate.capsule.mouseUp(with: lateRelease)
        try require(!delegate.details.isVisible,
                    "A release from a gesture cancelled by hiding cannot open details after recovery")
        delegate.toggleDetails()
        let detailLabels = delegate.details.stack.arrangedSubviews.compactMap { ($0 as? NSTextField)?.stringValue }
        try require(detailLabels.contains("部分状态待确认") && detailLabels.contains("待处理状态待确认"),
                    "Explicit details retain the coverage explanation hidden from the capsule")

        apply(fixture(pending: 2, unknown: 1, inboxUnknown: 1))
        try require(delegate.panel.isVisible && delegate.controller.status.text == "2 待处理",
                    "Pending-only activity shows no zero execution or unknown label")
        try require(delegate.capsule.shine.animation(forKey: "shine") == nil,
                    "Pending-only activity has no execution shine")
        apply(fixture(active: 3, pending: 2, unknown: 4, inboxUnknown: 1))
        try require(delegate.panel.isVisible && delegate.controller.status.text == "3 执行 · 2 待处理",
                    "Both confirmed categories remain visible alongside unknown coverage")
        apply(fixture(childActive: 7, errors: 5))
        try hiddenWithoutContent("Child execution and ordinary errors do not invent main activity")

        apply(fixture(active: 2, pending: 1))
        delegate.toggleDetails()
        try require(delegate.details.isVisible, "Activity details can reopen after automatic recovery")
        apply(delegate.controller.snapshot, error: "人工读取失败")
        try hiddenWithoutContent("A failed refresh hides stale activity and closes details")
        try require(delegate.controller.snapshot?.counts.active == 2,
                    "Failed refresh retains the previous snapshot without presenting it as current")
        apply(fixture(active: 2), age: 16)
        try hiddenWithoutContent("Expired state hides the capsule without an update placeholder")
        apply(nil)
        try hiddenWithoutContent("Missing initial state shows no connecting capsule")
        apply(fixture(active: 2))
        try require(delegate.panel.isVisible && !delegate.details.isVisible,
                    "A fresh later observation restores only the capsule")

        try menu("暂隐胶囊", action: #selector(AppDelegate.hide), delegate: delegate)
        apply(fixture())
        apply(fixture(active: 4, pending: 3))
        try require(!delegate.panel.isVisible && !delegate.details.isVisible && delegate.controller.hidden,
                    "New work after quiet activity never reverses the user's hide")
        delegate.toggleDetails()
        try require(!delegate.details.isVisible, "A manually hidden capsule cannot open details through a stale click")
        try require(delegate.preferences.bool(forKey: "hidden"), "Manual hiding retains its saved preference")
        apply(fixture())
        try menu("显示胶囊", action: #selector(AppDelegate.show), delegate: delegate)
        try hiddenWithoutContent("Show with no activity clears the override without displaying an empty capsule")
        try require(!delegate.controller.hidden && !delegate.preferences.bool(forKey: "hidden"),
                    "Show re-enables future automatic appearances")
        try menu("重置位置", action: #selector(AppDelegate.reset), delegate: delegate)
        try hiddenWithoutContent("Reset position with no activity does not force a visible quota shell")
        apply(fixture(pending: 1))
        try require(delegate.panel.isVisible && !delegate.panel.isKeyWindow && !delegate.panel.isMainWindow,
                    "New pending activity reappears after the user explicitly re-enables showing")
    }

    private static func verifySessionCardCallbacks(nextTo capsule: NSPanel) throws {
        let panel = DetailsPanel()
        defer { panel.orderOut(nil) }
        var captured: [String] = []
        func render(_ ids: [String]) {
            let offset = panel.beginUpdate()
            for id in ids {
                panel.session(name: "人工卡片 " + id, summary: "人工回调 · 不连接 API", warning: false,
                              enabled: true, identifier: "session:" + id) { captured.append(id) }
            }
            panel.endUpdate(offset: offset)
        }
        render(["artificial-a", "artificial-b"])
        panel.place(nextTo: capsule)
        panel.orderFront(nil)
        settle()
        let originalRows = panel.stack.arrangedSubviews.compactMap { $0 as? SessionRow }
        try require(originalRows.count == 2 && originalRows.allSatisfy { !$0.needsPanelToBecomeKey && !$0.acceptsFirstResponder },
                    "Whole cards accept clicks without requesting key focus")
        try require(!panel.isKeyWindow && !panel.isMainWindow, "Callback fixture panel does not take key or main focus")
        originalRows[0].performClick(nil)
        try require(captured == ["artificial-a"], "Whole-card click dispatches the current first session")
        guard let secondAction = originalRows[1].action else { throw Failure(message: "Whole card is missing its action") }
        try require(NSApp.sendAction(secondAction, to: originalRows[1].target, from: originalRows[1]),
                    "Whole-card action is dispatched through its button target")
        try require(captured == ["artificial-a", "artificial-b"], "Second card callback targets its own session")
        render(["artificial-c", "artificial-d"])
        let reusedRows = panel.stack.arrangedSubviews.compactMap { $0 as? SessionRow }
        try require(reusedRows.count == 2 && reusedRows[0] === originalRows[0] && reusedRows[1] === originalRows[1],
                    "Details reuse the existing card pool")
        reusedRows[0].performClick(nil)
        reusedRows[1].performClick(nil)
        try require(captured == ["artificial-a", "artificial-b", "artificial-c", "artificial-d"],
                    "Reused cards invoke new sessions and never retain old callback identities")
        var readCount = 0
        reusedRows[0].configureRead(version: "artificial-c:1", action: { readCount += 1 })
        try require(reusedRows[0].subviews.filter { $0 is NSButton }.count == 1,
                    "A card has only its navigation target and no inline confirmation buttons")
        let rightClick = NSEvent.mouseEvent(with: .rightMouseDown, location: .zero, modifierFlags: [], timestamp: 0,
                                           windowNumber: panel.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1)!
        let wasActive = NSApp.isActive
        reusedRows[0].rightMouseDown(with: rightClick)
        try require(readCount == 1 && captured.count == 4, "One right click marks a completion read without navigating")
        try require(!reusedRows[0].summaryLabel.isHidden, "Right-click removal never replaces the summary with a second-step control")
        reusedRows[0].configureRead(version: "artificial-c:1", action: nil)
        reusedRows[0].rightMouseDown(with: rightClick)
        try require(readCount == 1, "A busy completion cannot repeat its right-click action")
        reusedRows[0].configureRead(version: nil, action: nil)
        reusedRows[0].rightMouseDown(with: rightClick)
        try require(readCount == 1, "Reusing a completion as an execution row discards its old read action")
        var dismissCount = 0
        let row = reusedRows[0]
        row.configureDismiss(version: "artificial-c:batch-a", action: { dismissCount += 1 })
        row.navigationButton.rightMouseDown(with: rightClick)
        try require(dismissCount == 1 && readCount == 1 && captured.count == 4,
                    "One right click removes a human reminder without navigation or completion read")
        try require(row.subviews.filter { $0 is NSButton }.count == 1 && !row.summaryLabel.isHidden,
                    "A human card uses no popup menu or inline second-click control")
        row.configureRead(version: nil, action: nil)
        row.configureDismiss(version: "artificial-c:batch-a", action: nil)
        row.rightMouseDown(with: rightClick)
        try require(dismissCount == 1, "A busy human batch cannot dispatch another removal")
        row.configureDismiss(version: "artificial-c:batch-b", action: { dismissCount += 10 })
        row.rightMouseDown(with: rightClick)
        try require(dismissCount == 11, "A reused human row dispatches only the current batch on one right click")
        row.configureDismiss(version: nil, action: nil)
        row.rightMouseDown(with: rightClick)
        try require(dismissCount == 11 && readCount == 1 && captured.count == 4,
                    "Legacy, resolved, and pure execution rows have no right-click removal")
        row.configureRead(version: nil, action: { readCount += 100 })
        row.configureDismiss(version: nil, action: { dismissCount += 100 })
        row.rightMouseDown(with: rightClick)
        try require(dismissCount == 11 && readCount == 1, "An action without a current version cannot run")
        row.configureRead(version: "artificial-c:2", action: { readCount += 10 })
        row.configureDismiss(version: nil, action: nil)
        row.navigationButton.rightMouseDown(with: rightClick)
        try require(readCount == 11 && captured.count == 4, "Reusing a human row as a completion dispatches only the current completion")
        try require(reusedRows[0].accessibilityIdentifier() == "session:artificial-c" &&
                    reusedRows[1].accessibilityIdentifier() == "session:artificial-d", "Reused cards refresh their accessible session identities")
        try require(!panel.isKeyWindow && !panel.isMainWindow && !capsule.isKeyWindow && !capsule.isMainWindow,
                    "Card actions preserve non-key and non-main windows")
        try require(NSApp.isActive == wasActive, "Right-click actions do not activate the application")
    }
    private static func verifySessionNames(nextTo capsule: NSPanel) throws {
        let panel = DetailsPanel()
        defer { panel.orderOut(nil) }
        let longProject = String(repeating: "人工项目", count: 15)
        let longSession = String(repeating: "人工会话", count: 15)
        let family = "👨‍👩‍👧‍👦"
        let cards = [
            SessionCard(id: "artificial-project", tool: "codex", state: "idle", role: "main",
                        alias: "人工别名", sessionTitle: "人工会话", projectDisplayName: longProject, projectName: "人工工作目录"),
            SessionCard(id: "artificial-session", tool: "codex", state: "idle", role: "main",
                        sessionTitle: longSession),
            SessionCard(id: "artificial-emoji", tool: "codex", state: "idle", role: "main",
                        projectDisplayName: String(repeating: family, count: 60)),
            SessionCard(id: "artificial-short", tool: "codex", state: "idle", role: "main",
                        projectDisplayName: "人工短项目")
        ]
        let summary = "人工状态 · 独立摘要 · 相对时间"
        func render(_ cards: [SessionCard]) {
            let offset = panel.beginUpdate()
            for card in cards {
                panel.session(name: card.displayName, summary: summary, warning: false, enabled: false,
                              identifier: card.id) {}
            }
            panel.endUpdate(offset: offset)
        }
        render(cards); panel.place(nextTo: capsule); panel.orderFront(nil); settle()
        let wasActive = NSApp.isActive
        let rows = panel.stack.arrangedSubviews.compactMap { $0 as? SessionRow }
        try require(rows.count == 4, "Artificial long project, session, emoji, and short names render")
        try require(rows[0].nameLabel.stringValue == longProject && rows[1].nameLabel.stringValue == longSession,
                    "A formal project is shown in full, and a full session name is used when no project exists")
        try require(rows[2].nameLabel.stringValue == String(repeating: family, count: 60),
                    "Full emoji names retain every original composed Character without an ellipsis")
        try require(rows.allSatisfy { $0.nameLabel.maximumNumberOfLines == 0 &&
                    $0.nameLabel.lineBreakMode == .byWordWrapping },
                    "Name fields wrap naturally instead of imposing a line or character limit")
        let shortHeight = rows[3].nameLabel.frame.height
        try require(rows[0].nameLabel.frame.height > shortHeight && rows[1].nameLabel.frame.height > shortHeight &&
                    rows[2].nameLabel.frame.height > shortHeight,
                    "Long project, session, and emoji text actually occupy multiple rendered lines")
        try require(rows.allSatisfy { row in
            row.summaryLabel.stringValue == summary &&
                !row.nameLabel.frame.intersects(row.summaryLabel.frame) &&
                row.bounds.contains(row.nameLabel.frame) && row.bounds.contains(row.summaryLabel.frame)
        }, "Each independently readable summary stays outside the name and inside its expanded row")
        try require(rows.allSatisfy { row in
            guard let cell = row.nameLabel.cell else { return false }
            let needed = cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: row.nameLabel.frame.width, height: 100_000)).height
            return row.nameLabel.frame.height + 0.5 >= needed && row.nameLabel.frame.maxX <= row.bounds.maxX
        }, "Rendered name height fits its full wrapped content without widening the detail panel")
        try require(rows.allSatisfy { row in
            row.toolTip == nil && row.navigationButton.toolTip == nil && row.nameLabel.toolTip == nil &&
                row.summaryLabel.toolTip == nil && row.trackingAreas.isEmpty
        }, "Rows retain no name tooltips or mouse-hover tracking")
        try require(panel.contentView?.subviews.count == 1 && panel.contentView?.subviews.first === panel.scroll,
                    "The detail panel contains only its scroll content, with no name-hover layer")
        try require(rows[0].navigationButton.accessibilityLabel() == longProject + "，" + summary &&
                    rows[2].navigationButton.accessibilityLabel() == cards[2].displayName + "，" + summary,
                    "Assistive labels retain the same complete names and independent summaries")
        let oldHeight = rows[0].frame.height
        let replacement = SessionCard(id: "artificial-replacement", tool: "codex", state: "idle", role: "main",
                                      alias: "更新后的人工别名", sessionTitle: "不优先的会话名")
        render([replacement]); settle()
        let reused = panel.stack.arrangedSubviews.compactMap { $0 as? SessionRow }
        try require(reused.count == 1 && reused[0] === rows[0] &&
                    reused[0].nameLabel.stringValue == replacement.displayName && reused[0].frame.height < oldHeight,
                    "Reusing a long row replaces the complete name and naturally shrinks its height")
        try require(reused[0].summaryLabel.stringValue == summary &&
                    !reused[0].nameLabel.frame.intersects(reused[0].summaryLabel.frame) &&
                    reused[0].navigationButton.accessibilityLabel() == replacement.displayName + "，" + summary &&
                    reused[0].toolTip == nil && reused[0].trackingAreas.isEmpty,
                    "A reused row refreshes its accessible name and summary without stale hover behavior")
        try require(!panel.isKeyWindow && !panel.isMainWindow && !capsule.isKeyWindow && !capsule.isMainWindow &&
                    NSApp.isActive == wasActive, "Full-name rendering and row reuse preserve passive panels")
    }
    private static func verifyScrollVisibility(nextTo capsule: NSPanel) throws {
        let panel = DetailsPanel()
        defer { panel.orderOut(nil) }
        let offset = panel.beginUpdate()
        for index in 0..<24 {
            panel.session(name: "人工滚动卡 \(index)", summary: "仅人工数据", warning: false,
                          enabled: false, identifier: "artificial-scroll:\(index)") {}
        }
        panel.endUpdate(offset: offset)
        panel.place(nextTo: capsule); panel.orderFront(nil); settle()
        let scroll = panel.scroll
        let wasActive = NSApp.isActive
        let originalWidth = scroll.contentView.bounds.width
        try require(panel.stack.frame.height > scroll.contentView.bounds.height,
                    "Scroll fixture contains actual overflowing document content")
        try require(!scroll.indicatorVisible && !scroll.hasVerticalScroller && !scroll.hasHorizontalScroller,
                    "Idle details contain no visible or reserved native scrollbar")
        scroll.scrollerStyle = .legacy
        panel.contentView?.layoutSubtreeIfNeeded(); settle()
        try require(!scroll.indicatorVisible && abs(scroll.contentView.bounds.width - originalWidth) < 0.1,
                    "Always-visible legacy style cannot reveal a gutter or shift content")
        let center = NotificationCenter.default
        center.post(name: NSScrollView.willStartLiveScrollNotification, object: scroll)
        try require(scroll.indicatorVisible && scroll.indicatorProportion > 0 && scroll.indicatorProportion < 1,
                    "Live scrolling shows an overlay with a document-length proportion")
        settle(seconds: 0.6)
        try require(scroll.indicatorVisible, "A live scroll keeps its indicator visible until the gesture ends")
        let maximum = panel.stack.frame.height - scroll.contentView.bounds.height
        scroll.contentView.scroll(to: NSPoint(x: 0, y: maximum * 0.4))
        scroll.reflectScrolledClipView(scroll.contentView)
        try require(abs(scroll.indicatorFraction - 0.4) < 0.01,
                    "The overlay tracks the actual document scroll position")
        try require(abs(scroll.contentView.bounds.width - originalWidth) < 0.1,
                    "Showing the overlay does not change the card content width")
        center.post(name: NSScrollView.didEndLiveScrollNotification, object: scroll)
        settle(seconds: 0.6)
        try require(!scroll.indicatorVisible, "The scrollbar disappears after live scrolling ends")

        func wheel(_ delta: Int32, momentum: Int64 = 0) throws -> NSEvent {
            guard let source = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 1,
                                       wheel1: delta, wheel2: 0, wheel3: 0) else {
                throw Failure(message: "Could not create an artificial scroll-wheel event")
            }
            source.setIntegerValueField(.scrollWheelEventMomentumPhase, value: momentum)
            guard let result = NSEvent(cgEvent: source) else { throw Failure(message: "Could not wrap an artificial wheel event") }
            return result
        }
        let initialOffset = scroll.contentView.bounds.minY
        scroll.scrollWheel(with: try wheel(-60))
        // AppKit commits precise wheel movement on a following run-loop turn.
        settle()
        try require(scroll.indicatorVisible && scroll.contentView.bounds.minY > initialOffset,
                    "Ordinary wheel scrolling still moves content and displays the indicator")
        settle(seconds: 0.6)
        try require(!scroll.indicatorVisible, "A wheel event without gesture phases also hides after settling")
        scroll.scrollWheel(with: try wheel(-15, momentum: 1))
        settle(seconds: 0.6)
        try require(scroll.indicatorVisible, "Momentum retains the scrollbar while inertia is active")
        scroll.scrollWheel(with: try wheel(0, momentum: 3))
        settle(seconds: 0.6)
        try require(!scroll.indicatorVisible, "The scrollbar hides after inertia ends")

        center.post(name: NSScrollView.willStartLiveScrollNotification, object: scroll)
        let indicator = scroll.indicatorView
        func mouse(_ type: NSEvent.EventType, y: CGFloat) throws -> NSEvent {
            let point = indicator.convert(NSPoint(x: indicator.bounds.midX, y: y), to: nil)
            guard let result = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: 0,
                                                 windowNumber: panel.windowNumber, context: nil, eventNumber: 1,
                                                 clickCount: 1, pressure: 1) else {
                throw Failure(message: "Could not create an artificial scrollbar drag event")
            }
            return result
        }
        let down = try mouse(.leftMouseDown, y: indicator.bounds.height * 0.2)
        let drag = try mouse(.leftMouseDragged, y: indicator.bounds.height * 0.85)
        let up = try mouse(.leftMouseUp, y: indicator.bounds.height * 0.85)
        NSApp.postEvent(drag, atStart: false); NSApp.postEvent(up, atStart: false)
        indicator.mouseDown(with: down)
        try require(scroll.indicatorFraction > 0.7 && scroll.contentView.bounds.minY > maximum * 0.7,
                    "The visible overlay remains draggable and updates the real document position")
        center.post(name: NSScrollView.didEndLiveScrollNotification, object: scroll)
        settle(seconds: 0.6)
        try require(!scroll.indicatorVisible && abs(scroll.contentView.bounds.width - originalWidth) < 0.1,
                    "The dragged scrollbar returns to hidden without horizontal content movement")
        try require(indicator.hitTest(NSPoint(x: indicator.frame.midX, y: indicator.frame.midY)) == nil,
                    "A hidden overlay cannot intercept card clicks")
        try require(!panel.isKeyWindow && !panel.isMainWindow && !capsule.isKeyWindow && !capsule.isMainWindow,
                    "Scrolling and scrollbar dragging preserve non-key and non-main panels")
        try require(NSApp.isActive == wasActive, "Scrolling and scrollbar dragging do not activate the application")
    }
    private static func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
        guard condition() else { throw Failure(message: message) }
        checks += 1
    }
    private static func menu(_ title: String, action: Selector, delegate: AppDelegate) throws {
        guard let item = delegate.statusItem.menu?.items.first(where: { $0.title == title }) else {
            throw Failure(message: "Required recovery menu item is missing")
        }
        try require(item.action == action && item.target === delegate, "Recovery menu selector and target are wired")
        try require(NSApp.sendAction(action, to: item.target, from: item), "Recovery menu action is dispatched")
    }
    private static func contained(_ frame: NSRect) -> Bool {
        NSScreen.screens.contains { screen in
            let visible = screen.visibleFrame.insetBy(dx: 6, dy: 6)
            return visible.contains(frame)
        }
    }
    private static func settle(seconds: TimeInterval = 0.12) {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline { _ = RunLoop.main.run(mode: .default, before: deadline) }
    }
    private static func fail(_ message: String) -> Never {
        fputs("Lifecycle integration refused: \(message)\n", stderr)
        exit(1)
    }
}
