import Foundation

public enum QuotaFreshness: String, Equatable {
    case valid, stale, unknown
}

public struct QuotaPresentation: Equatable {
    public let fraction: Double?
    public let freshness: QuotaFreshness
    public let description: String

    public static func evaluate(snapshot: SubscriptionSnapshot?, selection: QuotaIdentity?, failed: Bool, now: TimeInterval) -> QuotaPresentation {
        guard let selection else { return unknown("尚未选择额度来源") }
        guard now.isFinite, let snapshot else { return unknown("额度尚未获取") }
        guard let platform = snapshot.platforms.first(where: { $0.tool == selection.tool }),
              let window = platform.limits.first(where: { $0.bucket == selection.bucket && $0.key == selection.key }) else {
            return unknown("已选额度来源尚未报告；保持原选择")
        }
        guard ["available", "stale", "error"].contains(platform.status) else {
            return unknown(platform.status == "auth_required" ? "额度需要授权" : "额度尚未报告")
        }
        let isPageSnapshot = window.source == "cursor_dashboard_ui"
        let expiredMessage = isPageSnapshot ? "进入可能重置日期，等待新快照" : "额度窗口已重置，等待新报告"
        if let validUntil = window.validUntil {
            guard validUntil.isFinite else { return unknown("额度快照有效截止未知") }
            if now >= validUntil { return unknown("进入可能重置日期，等待新快照") }
        }
        guard window.status != "expired" else { return unknown(expiredMessage) }
        if let reset = window.resetsAt {
            guard reset.isFinite else { return unknown("额度重置时间未知") }
            if now >= reset { return unknown(expiredMessage) }
        }
        // A page observation establishes only capture freshness. It never fills
        // updatedAt or pretends the provider supplied an update timestamp.
        let observedAt = isPageSnapshot ? (window.observedAt ?? window.capturedAt ?? platform.capturedAt) : window.observedAt
        guard ["available", "stale"].contains(window.status),
              let observed = observedAt, observed.isFinite, !isPageSnapshot || observed > 0,
              ["percent", "credits"].contains(window.unit),
              let fraction = usedFraction(window) else { return unknown("额度口径或来源时间未知") }
        let isStale = failed || platform.status == "stale" || platform.status == "error" ||
            window.status == "stale" || now - observed > 900 || observed > now + 60
        if isPageSnapshot {
            let percent = String(format: "%.0f", fraction * 100)
            let state = isStale ? "页面快照 · 旧快照" : "页面快照"
            let reset = window.resetLabel?.trimmingCharacters(in: .whitespacesAndNewlines)
            let resetDescription = reset.map { $0.isEmpty ? "" : "；\($0)" } ?? ""
            let error = platform.status == "error" || failed ? "；读取失败，保留页面值" : ""
            return QuotaPresentation(fraction: fraction, freshness: isStale ? .stale : .valid,
                                     description: "\(platform.name) · \(window.label) · \(state) · 约已用 \(percent)%\(resetDescription)\(error)")
        }
        let percent = String(format: "%.1f", fraction * 100)
        let state = isStale ? "旧快照" : "已用"
        let error = platform.status == "error" || failed ? "；查询失败，保留旧值" : ""
        return QuotaPresentation(fraction: fraction, freshness: isStale ? .stale : .valid,
                                 description: "\(platform.name) · \(window.label) · \(state) \(percent)%\(error)")
    }

    private static func unknown(_ reason: String) -> QuotaPresentation {
        QuotaPresentation(fraction: nil, freshness: .unknown, description: reason)
    }

    private static func usedFraction(_ window: SubscriptionLimit) -> Double? {
        if let remaining = window.remainingPercent, remaining.isFinite {
            return min(1, max(0, 1 - remaining / 100))
        }
        if let used = window.used, let limit = window.limit,
           used.isFinite, limit.isFinite, used >= 0, limit > 0 {
            let ratio = used / limit
            if ratio.isFinite { return min(1, max(0, ratio)) }
        }
        return nil
    }
}

/// A missing selected source never silently changes the meaning of the outline.
public struct QuotaSelection: Equatable {
    public private(set) var identity: QuotaIdentity?
    public private(set) var automaticallySelectWeekly: Bool

    public init(identity: QuotaIdentity? = nil, automaticallySelectWeekly: Bool = true) {
        self.identity = identity
        self.automaticallySelectWeekly = automaticallySelectWeekly && identity == nil
    }

    @discardableResult
    public mutating func resolve(snapshot: SubscriptionSnapshot?, now: TimeInterval) -> QuotaIdentity? {
        guard identity == nil, automaticallySelectWeekly, let snapshot else { return identity }
        let candidates = snapshot.platforms.filter { $0.tool == "codex" }.flatMap { platform in
            platform.limits.filter { $0.windowMinutes == 10080 }.map { QuotaIdentity(tool: platform.tool, limit: $0) }
        }.sorted { ($0.bucket, $0.key) < ($1.bucket, $1.key) }
        if let candidate = candidates.first(where: {
            QuotaPresentation.evaluate(snapshot: snapshot, selection: $0, failed: false, now: now).freshness == .valid
        }) {
            identity = candidate
            automaticallySelectWeekly = false
        }
        return identity
    }

    public mutating func select(_ identity: QuotaIdentity?) {
        self.identity = identity
        automaticallySelectWeekly = false
    }
}
