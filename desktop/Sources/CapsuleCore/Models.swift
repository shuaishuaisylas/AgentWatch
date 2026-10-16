import Foundation

/// Decode API responses using JSONDecoder.keyDecodingStrategy = .convertFromSnakeCase.
/// Required counters are intentionally not defaulted when a response is malformed.
public struct Counts: Codable, Equatable {
    public let active: Int
    public let waiting: Int
    public let error: Int
    public let unknown: Int
    public let observed: Int?
    public let online: Int?
    public let total: Int?
    public let unknownRoles: Int?

    public init(active: Int, waiting: Int, error: Int, unknown: Int,
                observed: Int? = nil, online: Int? = nil, total: Int? = nil,
                unknownRoles: Int? = 0) {
        self.active = active; self.waiting = waiting; self.error = error; self.unknown = unknown
        self.observed = observed; self.online = online; self.total = total; self.unknownRoles = unknownRoles
    }

    public var isValid: Bool {
        [active, waiting, error, unknown].allSatisfy { $0 >= 0 } &&
        [observed, online, total, unknownRoles].compactMap { $0 }.allSatisfy { $0 >= 0 }
    }
}

/// Complete root-session aggregates supplied before the server truncates its card list.
/// Categories can overlap and must not be added to reconstruct `pending`.
public struct AttentionCounts: Codable, Equatable {
    public let pending: Int
    public let waitingUser: Int
    public let waitingApproval: Int
    public let error: Int
    public let unknown: Int

    public init(pending: Int, waitingUser: Int = 0, waitingApproval: Int = 0,
                error: Int = 0, unknown: Int = 0) {
        self.pending = pending; self.waitingUser = waitingUser; self.waitingApproval = waitingApproval
        self.error = error; self.unknown = unknown
    }

    public var isValid: Bool { [pending, waitingUser, waitingApproval, error, unknown].allSatisfy { $0 >= 0 } }
}

/// A main card includes its explicitly associated visible subtree; a child card is local.
public struct SessionAttention: Codable, Equatable {
    public let pending: Bool
    public let waitingUser: Int
    public let waitingApproval: Int
    public let error: Int
    public let unknown: Int
    public let oldestAt: TimeInterval?
    /// Opaque version of the current unresolved human-request batch at this root.
    /// Older backends omit it; malformed versions disable manual dismissal only.
    public let dismissVersion: String?

    public init(pending: Bool, waitingUser: Int = 0, waitingApproval: Int = 0,
                error: Int = 0, unknown: Int = 0, oldestAt: TimeInterval? = nil,
                dismissVersion: String? = nil) {
        self.pending = pending; self.waitingUser = waitingUser; self.waitingApproval = waitingApproval
        self.error = error; self.unknown = unknown; self.oldestAt = oldestAt
        self.dismissVersion = dismissVersion
    }

    public var isValid: Bool {
        [waitingUser, waitingApproval, error, unknown].allSatisfy { $0 >= 0 } &&
            (oldestAt.map { $0.isFinite && $0 >= 0 } ?? true)
    }
}

/// Root union of required human actions and unread completed sessions. The two
/// subcounts can overlap; only `pending` is the complete capsule count.
public struct InboxCounts: Codable, Equatable {
    public let pending: Int
    public let actionRequired: Int
    public let unreadCompleted: Int
    public let unknown: Int

    public init(pending: Int, actionRequired: Int = 0, unreadCompleted: Int = 0, unknown: Int = 0) {
        self.pending = pending; self.actionRequired = actionRequired
        self.unreadCompleted = unreadCompleted; self.unknown = unknown
    }

    public var isValid: Bool { [pending, actionRequired, unreadCompleted, unknown].allSatisfy { $0 >= 0 } }
}

public struct SessionInbox: Codable, Equatable {
    public let unreadCompleted: Bool
    /// Exact completion version acknowledged only after successful session navigation.
    public let completedAt: TimeInterval?

    public init(unreadCompleted: Bool, completedAt: TimeInterval? = nil) {
        self.unreadCompleted = unreadCompleted; self.completedAt = completedAt
    }

    public var isValid: Bool {
        (completedAt.map { $0.isFinite && $0 >= 0 } ?? !unreadCompleted)
    }
}

public struct Snapshot: Codable, Equatable {
    public let demo: Bool
    public let now: TimeInterval
    public let counts: Counts
    public let childrenCounts: Counts?
    public let attentionCounts: AttentionCounts?
    public let inboxCounts: InboxCounts?
    public let sessions: [SessionCard]
    /// Independent complete recent-main query; nil means an older backend has no coverage.
    public let recentCompleted: [SessionCard]?
    public let truncated: Bool
    public let date: String?
    public let timezone: String?

    public init(demo: Bool, now: TimeInterval, counts: Counts, childrenCounts: Counts?,
                sessions: [SessionCard] = [], truncated: Bool = false,
                date: String? = nil, timezone: String? = nil,
                attentionCounts: AttentionCounts? = nil, recentCompleted: [SessionCard]? = nil,
                inboxCounts: InboxCounts? = nil) {
        self.demo = demo; self.now = now; self.counts = counts; self.childrenCounts = childrenCounts
        self.sessions = sessions; self.truncated = truncated; self.date = date; self.timezone = timezone
        self.attentionCounts = attentionCounts; self.recentCompleted = recentCompleted
        self.inboxCounts = inboxCounts
    }
}

public struct JumpInfo: Codable, Equatable {
    public let available: Bool
    public let label: String
    public let precision: String?
    public let kind: String
    public let reason: String?

    public init(available: Bool, label: String, precision: String? = nil,
                kind: String = "none", reason: String? = nil) {
        self.available = available; self.label = label; self.precision = precision
        self.kind = kind; self.reason = reason
    }
}

public struct SessionCard: Codable, Equatable, Identifiable {
    public let id: String
    public let tool: String
    public let state: String
    public let stale: Bool
    public let role: String?
    public let parentId: String?
    public let sourceId: String?
    public let alias: String?
    public let sessionTitle: String?
    /// Official application sidebar project name, distinct from a cwd basename.
    public let projectDisplayName: String?
    public let projectName: String?
    public let projectId: String?
    public let surface: String?
    public let stage: String?
    public let stateAt: TimeInterval?
    public let stateSource: String?
    public let stateAge: TimeInterval?
    public let liveness: String?
    public let coverage: String?
    public let workingDirectory: String?
    public let worktreeName: String?
    public let childrenSummary: Counts?
    public let attention: SessionAttention?
    public let lastCompletedAt: TimeInterval?
    public let inbox: SessionInbox?
    public let jump: JumpInfo?
    public let depth: Int?

    public init(id: String, tool: String, state: String, stale: Bool = false,
                role: String? = nil, parentId: String? = nil, sourceId: String? = nil,
                alias: String? = nil, sessionTitle: String? = nil, projectDisplayName: String? = nil,
                projectName: String? = nil, projectId: String? = nil,
                surface: String? = nil, stage: String? = nil, stateAt: TimeInterval? = nil,
                stateSource: String? = nil, stateAge: TimeInterval? = nil, liveness: String? = nil,
                coverage: String? = nil, workingDirectory: String? = nil, worktreeName: String? = nil,
                childrenSummary: Counts? = nil, jump: JumpInfo? = nil, depth: Int? = nil,
                attention: SessionAttention? = nil, lastCompletedAt: TimeInterval? = nil,
                inbox: SessionInbox? = nil) {
        self.id = id; self.tool = tool; self.state = state; self.stale = stale; self.role = role
        self.parentId = parentId; self.sourceId = sourceId; self.alias = alias
        self.sessionTitle = sessionTitle
        self.projectDisplayName = projectDisplayName
        self.projectName = projectName; self.projectId = projectId; self.surface = surface
        self.stage = stage; self.stateAt = stateAt; self.stateSource = stateSource
        self.stateAge = stateAge; self.liveness = liveness; self.coverage = coverage
        self.workingDirectory = workingDirectory; self.worktreeName = worktreeName
        self.childrenSummary = childrenSummary; self.jump = jump; self.depth = depth
        self.attention = attention; self.lastCompletedAt = lastCompletedAt
        self.inbox = inbox
    }

    public var confirmedActive: Bool {
        guard state == "active", !stale, !(stateAge.map { !$0.isFinite || $0 > 120 } ?? false) else { return false }
        return liveness == "online" ||
            (liveness != "offline" && ["hook", "event"].contains(stateSource ?? ""))
    }

    public var displayName: String {
        if let project = Self.cleanedProjectName(projectDisplayName) { return project }
        let directoryName = readableDirectoryName
        // Codex cwd names can be internal output folders. Its official session
        // identity is preferred when there is no verified sidebar project name.
        if tool != "codex", let directoryName { return directoryName }
        for candidate in [alias, sessionTitle] {
            if let value = Self.cleanedName(candidate) { return value }
        }
        return directoryName ?? "未命名会话"
    }

    private static func cleanedName(_ value: String?) -> String? {
        guard let name = value?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty else { return nil }
        return name
    }

    private static func cleanedProjectName(_ value: String?) -> String? {
        guard let project = cleanedName(value), !["未识别项目", "未知项目"].contains(project) else { return nil }
        return project
    }

    private var readableDirectoryName: String? {
        guard projectId?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() != "unknown",
              let project = Self.cleanedProjectName(projectName), !isInternalProjectDirectory(project) else { return nil }
        return project
    }

    private func isInternalProjectDirectory(_ project: String) -> Bool {
        guard project.range(of: #"^g-p-[0-9a-fA-F]{32}$"#, options: .regularExpression) != nil,
              let directory = workingDirectory,
              directory.hasPrefix("/") || directory.hasPrefix("~/") else { return false }
        let components = directory.split(separator: "/").map(String.init)
        return zip(components, components.dropFirst()).contains { parent, child in
            parent == ".chatgpt-projects" && child == project
        }
    }

    /// Only explicit server evidence establishes a human action; state/error alone cannot.
    public var requiresHumanAttention: Bool? {
        guard let attention, attention.isValid else { return nil }
        return attention.pending
    }

    /// Manual confirmation applies only to the exact root batch supplied by the
    /// server, never to legacy state guesses, unknown coverage, or child cards.
    public var attentionDismissVersion: String? {
        guard role == "main", requiresHumanAttention == true,
              let version = attention?.dismissVersion, version.utf8.count == 64,
              version.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else { return nil }
        return version
    }

    /// Missing read coverage is unknown; an idle state or completion time alone
    /// does not establish that a completion is still unread.
    public var unreadCompleted: Bool? {
        guard let inbox, inbox.isValid else { return nil }
        guard inbox.unreadCompleted else { return false }
        guard let completedAt = inbox.completedAt, completedAt == lastCompletedAt else { return nil }
        return true
    }

    func replacingInbox(_ value: SessionInbox) -> SessionCard {
        SessionCard(id: id, tool: tool, state: state, stale: stale, role: role, parentId: parentId,
                    sourceId: sourceId, alias: alias, sessionTitle: sessionTitle, projectDisplayName: projectDisplayName,
                    projectName: projectName, projectId: projectId,
                    surface: surface, stage: stage, stateAt: stateAt, stateSource: stateSource,
                    stateAge: stateAge, liveness: liveness, coverage: coverage,
                    workingDirectory: workingDirectory, worktreeName: worktreeName,
                    childrenSummary: childrenSummary, jump: jump, depth: depth,
                    attention: attention, lastCompletedAt: lastCompletedAt, inbox: value)
    }

    func replacingAttention(_ value: SessionAttention) -> SessionCard {
        SessionCard(id: id, tool: tool, state: state, stale: stale, role: role, parentId: parentId,
                    sourceId: sourceId, alias: alias, sessionTitle: sessionTitle, projectDisplayName: projectDisplayName,
                    projectName: projectName, projectId: projectId,
                    surface: surface, stage: stage, stateAt: stateAt, stateSource: stateSource,
                    stateAge: stateAge, liveness: liveness, coverage: coverage,
                    workingDirectory: workingDirectory, worktreeName: worktreeName,
                    childrenSummary: childrenSummary, jump: jump, depth: depth,
                    attention: value, lastCompletedAt: lastCompletedAt, inbox: inbox)
    }

    public var attentionLabels: [String] {
        guard let attention, attention.isValid else { return [] }
        var labels: [String] = []
        if attention.waitingUser > 0 { labels.append("待回复") }
        if attention.waitingApproval > 0 { labels.append("待批准") }
        if attention.pending && labels.isEmpty { labels.append("待处理") }
        return labels
    }

    public var stateLabel: String {
        switch state {
        case "active": return confirmedActive ? "执行中" : "执行待确认"
        case "waiting_user": return "待回复"
        case "waiting_approval": return "待批准"
        case "error": return "异常"
        case "idle": return "空闲"
        case "ended": return "运行结束"
        case "unknown": return "待确认"
        default: return "未知状态（\(state)）"
        }
    }
}

public struct ChildrenResponse: Codable, Equatable {
    public let demo: Bool
    public let now: TimeInterval
    public let parentId: String
    public let children: [SessionCard]
    public let counts: Counts
    public let truncated: Bool

    public init(demo: Bool, now: TimeInterval, parentId: String, children: [SessionCard], counts: Counts, truncated: Bool = false) {
        self.demo = demo; self.now = now; self.parentId = parentId; self.children = children
        self.counts = counts; self.truncated = truncated
    }
}

public struct SubscriptionSnapshot: Codable, Equatable {
    public let demo: Bool
    public let now: TimeInterval
    public let platforms: [SubscriptionPlatform]

    public init(demo: Bool, now: TimeInterval, platforms: [SubscriptionPlatform]) {
        self.demo = demo; self.now = now; self.platforms = platforms
    }
}

public struct SubscriptionPlatform: Codable, Equatable {
    public let tool: String
    public let name: String
    public let status: String
    public let updatedAt: TimeInterval?
    /// Local page capture time, distinct from an official source update time.
    public let capturedAt: TimeInterval?
    public let notice: String?
    public let refreshing: Bool?
    public let limits: [SubscriptionLimit]

    public init(tool: String, name: String, status: String, updatedAt: TimeInterval? = nil,
                capturedAt: TimeInterval? = nil, notice: String? = nil, refreshing: Bool? = nil,
                limits: [SubscriptionLimit]) {
        self.tool = tool; self.name = name; self.status = status; self.updatedAt = updatedAt
        self.capturedAt = capturedAt
        self.notice = notice; self.refreshing = refreshing; self.limits = limits
    }
}

public struct SubscriptionLimit: Codable, Equatable {
    public let key: String
    public let bucket: String
    public let label: String
    public let source: String
    public let observedAt: TimeInterval?
    public let displayPrecision: String?
    public let capturedAt: TimeInterval?
    /// Human-readable date precision supplied by the page; never a reset timestamp.
    public let resetLabel: String?
    /// Conservative validity cutoff for a date-only snapshot, not a reset time.
    public let validUntil: TimeInterval?
    public let unit: String
    public let status: String
    public let limit: Double?
    public let used: Double?
    public let remaining: Double?
    public let remainingPercent: Double?
    public let windowMinutes: Double?
    public let resetsAt: TimeInterval?

    public init(key: String, bucket: String, label: String, source: String,
                observedAt: TimeInterval?, unit: String = "percent", status: String = "available",
                limit: Double? = nil, used: Double? = nil, remaining: Double? = nil,
                remainingPercent: Double? = nil, windowMinutes: Double? = nil, resetsAt: TimeInterval? = nil,
                displayPrecision: String? = nil, capturedAt: TimeInterval? = nil, resetLabel: String? = nil,
                validUntil: TimeInterval? = nil) {
        self.key = key; self.bucket = bucket; self.label = label; self.source = source
        self.observedAt = observedAt; self.unit = unit; self.status = status
        self.displayPrecision = displayPrecision; self.capturedAt = capturedAt; self.resetLabel = resetLabel
        self.validUntil = validUntil
        self.limit = limit; self.used = used; self.remaining = remaining
        self.remainingPercent = remainingPercent; self.windowMinutes = windowMinutes; self.resetsAt = resetsAt
    }
}

public struct QuotaIdentity: Codable, Hashable {
    public let tool: String
    public let bucket: String
    public let key: String

    public init(tool: String, bucket: String, key: String) {
        self.tool = tool; self.bucket = bucket; self.key = key
    }

    public init(tool: String, limit: SubscriptionLimit) {
        self.init(tool: tool, bucket: limit.bucket, key: limit.key)
    }
}
