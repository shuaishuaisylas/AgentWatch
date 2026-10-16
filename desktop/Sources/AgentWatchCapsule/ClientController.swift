import AppKit
import CapsuleCore

@MainActor
final class ClientController {
    let baseURL: URL
    let expectDemo: Bool
    var onChange: (() -> Void)?
    var snapshot: Snapshot?
    var subscriptions: SubscriptionSnapshot?
    var children: ChildrenResponse?
    var selectedParent: String?
    var snapshotError: String?
    var subscriptionError: String?
    var childrenError: String?
    var actionMessage: String?
    var actionBusy = false
    var suspended = false
    var hidden = false
    var lastSuccess: TimeInterval?
    var lastChildrenSuccess: TimeInterval?
    private var client: APIClient?
    private var generation = 0
    private var snapshotGeneration = 0
    private var snapshotTask: Task<Void, Never>?
    private var subscriptionTask: Task<Void, Never>?
    private var childrenTask: Task<Void, Never>?
    private var lastSnapshotAttempt = -Double.infinity
    private var lastSubscriptionAttempt = -Double.infinity
    private var lastChildrenAttempt = -Double.infinity
    private var detailsVisible = false
    private var childrenGeneration = 0
    let probe: Bool

    init(baseURL: URL, expectDemo: Bool, probe: Bool, connection: APIClient? = nil) {
        self.baseURL = baseURL; self.expectDemo = expectDemo; self.probe = probe
        if probe {
            let t = Date().timeIntervalSince1970
            let main = SessionCard(id: "codex:artificial-main", tool: "codex", state: "active", role: "main", alias: "人工材质验证", stateAt: t, stateSource: "demo", liveness: "online", attention: SessionAttention(pending: true, waitingUser: 1, oldestAt: t - 120))
            let active = SessionCard(id: "codex:artificial-active", tool: "codex", state: "active", role: "main", alias: "文档校对", stateAt: t, stateSource: "demo", liveness: "online", attention: SessionAttention(pending: false))
            let other = SessionCard(id: "claude:artificial-active", tool: "claude", state: "active", role: "main", alias: "界面组件", stateAt: t, stateSource: "demo", liveness: "online", attention: SessionAttention(pending: false))
            let approval = SessionCard(id: "claude:artificial-approval", tool: "claude", state: "waiting_approval", role: "main", alias: "权限确认", stateAt: t, stateSource: "demo", liveness: "online", attention: SessionAttention(pending: true, waitingApproval: 1, oldestAt: t - 60))
            let complete = SessionCard(id: "codex:artificial-complete", tool: "codex", state: "idle", role: "main", alias: "完成记录示例", stateAt: t - 300, attention: SessionAttention(pending: false), lastCompletedAt: t - 300, inbox: SessionInbox(unreadCompleted: true, completedAt: t - 300))
            snapshot = Snapshot(demo: true, now: t, counts: Counts(active: 3, waiting: 1, error: 0, unknown: 0), childrenCounts: Counts(active: 0, waiting: 0, error: 0, unknown: 0, total: 0), sessions: [main, active, other, approval, complete], attentionCounts: AttentionCounts(pending: 2, waitingUser: 1, waitingApproval: 1), recentCompleted: [complete], inboxCounts: InboxCounts(pending: 3, actionRequired: 2, unreadCompleted: 1))
            lastSuccess = ProcessInfo.processInfo.systemUptime
            subscriptions = SubscriptionSnapshot(demo: true, now: t, platforms: [SubscriptionPlatform(tool: "codex", name: "Codex", status: "available", limits: [SubscriptionLimit(key: "artificial-week", bucket: "codex", label: "人工周窗口", source: "demo", observedAt: t, limit: 100, used: 38, remainingPercent: 62, windowMinutes: 10080, resetsAt: t + 86400)])])
        } else {
            do {
                if let connection {
                    guard connection.baseURL == baseURL else { throw APIClientError.invalidBaseURL }
                    client = connection
                } else { client = try APIClient(baseURL: baseURL) }
            }
            catch { snapshotError = "服务地址无效，仅允许固定本机地址" }
        }
    }
    var status: CapsuleStatus {
        CapsuleStatus.evaluate(snapshot: snapshot, failed: snapshotError != nil, age: lastSuccess.map { ProcessInfo.processInfo.systemUptime - $0 }, suspended: suspended)
    }
    func tick(force: Bool = false) {
        guard !suspended else { return }
        if probe { lastSuccess = ProcessInfo.processInfo.systemUptime; snapshotError = nil; subscriptionError = nil; onChange?(); return }
        guard let client else { return }
        let uptime = ProcessInfo.processInfo.systemUptime, interval = hidden ? 12.0 : 4.0
        let epoch = generation
        if snapshotTask == nil, force || uptime - lastSnapshotAttempt >= interval {
            lastSnapshotAttempt = uptime
            let snapshotEpoch = snapshotGeneration
            snapshotTask = Task { [weak self] in
                guard let self else { return }
                defer { if self.generation == epoch && self.snapshotGeneration == snapshotEpoch { self.snapshotTask = nil } }
                do {
                    let result = try await client.get("/api/snapshot?tool=all&project=all&role=main", as: Snapshot.self)
                    guard !Task.isCancelled, self.generation == epoch, self.snapshotGeneration == snapshotEpoch else { return }
                    guard result.demo == self.expectDemo else { throw ViewError.modeMismatch }
                    self.snapshot = result; self.snapshotError = nil; self.lastSuccess = ProcessInfo.processInfo.systemUptime
                } catch {
                    guard !Task.isCancelled, self.generation == epoch, self.snapshotGeneration == snapshotEpoch else { return }
                    self.snapshotError = "状态读取失败，保留上次快照"
                }
                self.onChange?()
            }
        }
        if subscriptionTask == nil, force || uptime - lastSubscriptionAttempt >= interval {
            lastSubscriptionAttempt = uptime
            subscriptionTask = Task { [weak self] in
                guard let self else { return }
                defer { if self.generation == epoch { self.subscriptionTask = nil } }
                do {
                    let result = try await client.get("/api/subscriptions", as: SubscriptionSnapshot.self)
                    guard !Task.isCancelled, self.generation == epoch else { return }
                    guard result.demo == self.expectDemo else { throw ViewError.modeMismatch }
                    self.subscriptions = result; self.subscriptionError = nil
                } catch {
                    guard !Task.isCancelled, self.generation == epoch else { return }
                    self.subscriptionError = "额度查询失败，保留旧值"
                }
                self.onChange?()
            }
        }
        if detailsVisible, selectedParent != nil, childrenTask == nil, force || uptime - lastChildrenAttempt >= 4 { loadChildren() }
        onChange?() // monotonic freshness deadline is evaluated even when requests hang.
    }
    func setDetailsVisible(_ visible: Bool) {
        detailsVisible = visible
        if !visible { childrenGeneration += 1; childrenTask?.cancel(); childrenTask = nil }
        else if selectedParent != nil { loadChildren() }
    }
    func selectParent(_ id: String) {
        childrenGeneration += 1
        childrenTask?.cancel(); childrenTask = nil
        selectedParent = selectedParent == id ? nil : id
        children = nil; childrenError = nil; lastChildrenSuccess = nil
        if selectedParent != nil { loadChildren() }
        onChange?()
    }
    private func loadChildren() {
        guard detailsVisible, childrenTask == nil, let id = selectedParent, let client else { return }
        let epoch = generation
        childrenGeneration += 1
        let childEpoch = childrenGeneration
        lastChildrenAttempt = ProcessInfo.processInfo.systemUptime
        var components = URLComponents(); components.queryItems = [URLQueryItem(name: "id", value: id)]
        let path = "/api/children?" + (components.percentEncodedQuery ?? "")
        childrenTask = Task { [weak self] in
            guard let self else { return }
            defer { if self.generation == epoch, self.childrenGeneration == childEpoch, self.selectedParent == id { self.childrenTask = nil } }
            do {
                let result = try await client.get(path, as: ChildrenResponse.self)
                guard !Task.isCancelled, self.generation == epoch, self.childrenGeneration == childEpoch, self.selectedParent == id, self.detailsVisible else { return }
                guard result.demo == self.expectDemo, result.parentId == id else { throw ViewError.modeMismatch }
                self.children = result; self.childrenError = nil; self.lastChildrenSuccess = ProcessInfo.processInfo.systemUptime
            } catch {
                guard !Task.isCancelled, self.generation == epoch, self.childrenGeneration == childEpoch, self.selectedParent == id, self.detailsVisible else { return }
                self.childrenError = "子 Agent 读取失败，保留旧值"
            }
            self.onChange?()
        }
    }
    func performJump(_ card: SessionCard) {
        guard !probe, !expectDemo, card.jump?.available == true else { return }
        let precise = ["指定会话", "会话路由"].contains(card.jump?.precision ?? "")
        let read = precise && card.unreadCompleted == true ? card.inbox?.completedAt.map { CompletionRead(id: card.id, at: $0) } : nil
        performAction("/api/jump", body: ["id": card.id], afterJump: read, expectedKind: card.jump?.kind)
    }
    func markCompletionRead(id: String, through: TimeInterval) {
        guard !probe, through.isFinite, through > 0 else { return }
        performAction("/api/inbox/read", body: ["id": id, "completed_at": String(through)],
                      markingRead: CompletionRead(id: id, at: through))
    }
    func confirmNoPending(_ card: SessionCard) {
        guard !probe, let version = card.attentionDismissVersion else { return }
        performAction("/api/attention/dismiss", body: ["id": card.id, "version": version],
                      dismissingAttention: AttentionDismiss(id: card.id, version: version))
    }
    func requestRefresh() { guard !probe else { return }; performAction("/api/rescan", body: [:]) }
    private struct CompletionRead { let id: String; let at: TimeInterval }
    private struct AttentionDismiss { let id: String; let version: String }
    private func performAction(_ path: String, body: [String: String], afterJump: CompletionRead? = nil,
                               expectedKind: String? = nil, markingRead: CompletionRead? = nil,
                               dismissingAttention: AttentionDismiss? = nil) {
        guard !actionBusy, let client else { return }
        actionBusy = true; actionMessage = "请求中"; onChange?()
        let epoch = generation
        Task { [weak self] in
            guard let self else { return }
            defer { if self.generation == epoch { self.actionBusy = false; self.onChange?() } }
            do {
                let result = try await client.post(path, body: body, as: ActionResponse.self)
                guard self.generation == epoch else { return }
                var acknowledged = result.readCompleted == true ? markingRead : nil
                if let afterJump, result.opened == true, result.kind == expectedKind {
                    // A received, precise navigation result precedes a separate
                    // versioned read receipt. An ambiguous jump is never replayed.
                    let receipt = try await client.post("/api/inbox/read", body: ["id": afterJump.id, "completed_at": String(afterJump.at)], as: ActionResponse.self)
                    guard self.generation == epoch else { return }
                    if receipt.readCompleted == true { acknowledged = afterJump }
                }
                if let dismissal = dismissingAttention, result.dismissed == true {
                    // Project only the exact clicked batch. A later request has
                    // a different version and must remain visible immediately.
                    self.snapshot = self.snapshot?.applyingAttentionDismiss(id: dismissal.id, version: dismissal.version)
                    self.snapshotGeneration += 1
                    self.snapshotTask?.cancel(); self.snapshotTask = nil
                    self.actionMessage = "已移除所选本地提醒"
                } else if let acknowledged {
                    self.snapshot = self.snapshot?.applyingCompletionRead(id: acknowledged.id, through: acknowledged.at)
                    // Reject GETs launched before the receipt; they must not put
                    // the old unread card back after the user just cleared it.
                    self.snapshotGeneration += 1
                    self.snapshotTask?.cancel(); self.snapshotTask = nil
                    self.actionMessage = "已标为看过"
                } else {
                    self.actionMessage = result.message ?? (dismissingAttention != nil ? "本地提醒未确认清除，请刷新确认" : result.requested == true ? "已请求刷新，等待采集结果" : markingRead != nil ? "已读状态未确认，请刷新查看" : "已请求原应用跳转，请在原应用确认")
                }
                self.tick(force: true)
            } catch {
                guard self.generation == epoch else { return }
                if dismissingAttention != nil {
                    if error as? APIClientError == .httpStatus(409) {
                        self.actionMessage = "提醒已变化，已保留；请刷新确认后再操作"
                    } else {
                        self.actionMessage = "本地提醒清除结果未确认；未自动重试，请刷新确认"
                    }
                } else {
                    self.actionMessage = "操作未确认完成；未自动重试，请检查后再操作"
                }
            }
        }
    }
    func sleep() {
        suspended = true; invalidateRequests(); onChange?()
    }
    func wake() {
        invalidateRequests()
        suspended = false; snapshotError = "恢复后等待新状态"; subscriptionError = "等待更新额度"
        lastSuccess = nil; children = nil
        if !probe { client = try? APIClient(baseURL: baseURL) }
        tick(force: true)
    }
    func stop() { invalidateRequests() }
    private func invalidateRequests() {
        generation += 1
        snapshotGeneration += 1
        childrenGeneration += 1
        if actionBusy { actionMessage = "操作结果未确认，未自动重试" }
        actionBusy = false
        snapshotTask?.cancel(); subscriptionTask?.cancel(); childrenTask?.cancel()
        snapshotTask = nil; subscriptionTask = nil; childrenTask = nil
        if let client { Task { await client.close() } }
        client = nil
    }
}
private enum ViewError: Error { case modeMismatch }
private struct ActionResponse: Decodable { let requested: Bool?; let opened: Bool?; let message: String?; let kind: String?; let readCompleted: Bool?; let dismissed: Bool? }
