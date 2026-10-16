import Foundation

public struct CapsuleStatus: Equatable {
    public let text: String
    public let active: Int?
    public let pending: Int?
    public let hasCoverageGap: Bool
    public let fresh: Bool
    public var hasContent: Bool { !text.isEmpty }

    public static func evaluate(snapshot: Snapshot?, failed: Bool, age: TimeInterval?, suspended: Bool = false) -> CapsuleStatus {
        guard let snapshot else {
            return CapsuleStatus(text: "",
                                 active: nil, pending: nil, hasCoverageGap: true, fresh: false)
        }
        let main = snapshot.counts
        let child = snapshot.childrenCounts
        let attention = snapshot.attentionCounts
        let inbox = snapshot.inboxCounts
        let pending: Int?
        if let inbox, inbox.isValid, !(inbox.pending == 0 && inbox.unknown > 0) {
            pending = inbox.pending
        } else {
            pending = nil
        }
        let gap = child == nil || main.unknown > 0 || (child?.unknown ?? 0) > 0 ||
            main.unknownRoles == nil || (main.unknownRoles ?? 0) > 0 ||
            pending == nil || (attention?.unknown ?? 0) > 0 || (inbox?.unknown ?? 0) > 0
        let ageIsFresh = age.map { $0.isFinite && $0 >= 0 && $0 < 15 } ?? false
        guard !failed, !suspended, ageIsFresh, main.isValid, child?.isValid != false else {
            return CapsuleStatus(text: "", active: nil, pending: nil, hasCoverageGap: gap, fresh: false)
        }
        // Execution is main-session only. Inbox is a separate server root union,
        // never waiting + error, child counts, or the possibly truncated cards.
        let active = main.active
        // Compact content is only confirmed work. Unknown values and coverage
        // remain available to details without creating a status-only capsule.
        var labels: [String] = []
        if active > 0 { labels.append("\(active) 执行") }
        if let pending, pending > 0 { labels.append("\(pending) 待处理") }
        let text = labels.joined(separator: " · ")
        return CapsuleStatus(text: text, active: active, pending: pending, hasCoverageGap: gap, fresh: true)
    }
}

/// Group only explicitly main-session cards. A row appears once, with attention
/// taking priority over execution; the card still retains both independent labels.
public struct CapsuleDetailGroups: Equatable {
    public let pending: [SessionCard]
    public let active: [SessionCard]
    public let completed: [SessionCard]
    public let isTruncated: Bool
    public let hasAttentionCoverageGap: Bool
    public let hasCompletionCoverage: Bool
    public let hasInboxCoverageGap: Bool

    public static func evaluate(snapshot: Snapshot?, now: TimeInterval) -> CapsuleDetailGroups {
        guard let snapshot else {
            return CapsuleDetailGroups(pending: [], active: [], completed: [], isTruncated: false,
                                       hasAttentionCoverageGap: true, hasCompletionCoverage: false,
                                       hasInboxCoverageGap: true)
        }
        var seen = Set<String>()
        let main = snapshot.sessions.filter { $0.role == "main" && seen.insert($0.id).inserted }
        let pending = main.filter { $0.requiresHumanAttention == true }
        let active = main.filter { $0.requiresHumanAttention != true && $0.confirmedActive }
        let current = Dictionary(uniqueKeysWithValues: main.map { ($0.id, $0) })
        let occupied = Set((pending + active).map(\.id))
        var completedIDs = Set<String>()
        let completed = (snapshot.recentCompleted ?? [])
            .filter { card in
                guard card.role == "main", !occupied.contains(card.id),
                      let completedAt = card.lastCompletedAt,
                      now.isFinite, completedAt.isFinite, completedAt >= now - 86_400,
                      completedAt <= now else { return false }
                let latest = current[card.id] ?? card
                // A new active round or unknown attention cannot turn the previous
                // completion into a claim that the session currently needs no work.
                return ["idle", "ended"].contains(latest.state) &&
                    latest.requiresHumanAttention == false && latest.attention?.unknown == 0 &&
                    card.unreadCompleted == true && latest.unreadCompleted == true &&
                    latest.inbox?.completedAt == card.inbox?.completedAt
            }
            .sorted {
                if $0.lastCompletedAt != $1.lastCompletedAt { return ($0.lastCompletedAt ?? 0) > ($1.lastCompletedAt ?? 0) }
                return $0.id < $1.id
            }
            .filter { completedIDs.insert($0.id).inserted }
            .prefix(5)
        let attentionGap = snapshot.attentionCounts?.isValid != true ||
            (snapshot.attentionCounts?.unknown ?? 0) > 0 || main.contains { $0.requiresHumanAttention == nil }
        return CapsuleDetailGroups(pending: pending, active: active, completed: Array(completed),
                                   isTruncated: snapshot.truncated, hasAttentionCoverageGap: attentionGap,
                                   hasCompletionCoverage: snapshot.recentCompleted != nil &&
                                       !(snapshot.recentCompleted ?? []).contains { $0.unreadCompleted == nil },
                                   hasInboxCoverageGap: snapshot.inboxCounts?.isValid != true ||
                                       (snapshot.inboxCounts?.unknown ?? 0) > 0 || main.contains { $0.unreadCompleted == nil })
    }
}

public enum CompletionRecency {
    public static func label(at: TimeInterval?, now: TimeInterval) -> String? {
        guard let at, at.isFinite, now.isFinite, at <= now, at >= now - 86_400 else { return nil }
        let age = now - at
        if age < 60 { return "刚完成" }
        if age < 3_600 { return "\(Int(age / 60)) 分钟前" }
        return "\(Int(age / 3_600)) 小时前"
    }
}

extension Snapshot {
    /// Dismiss one acknowledged human-request batch without interpreting it as a
    /// real reply/approval, altering execution, or clearing any completion read.
    public func applyingAttentionDismiss(id: String, version: String) -> Snapshot {
        let roots = sessions.filter { $0.id == id && $0.role == "main" }
        guard let root = roots.first, let attention = root.attention,
              root.attentionDismissVersion == version,
              roots.allSatisfy({ $0.attentionDismissVersion == version }) else { return self }
        func clear(_ card: SessionCard) -> SessionCard {
            guard card.id == id, card.attentionDismissVersion == version, let previous = card.attention else { return card }
            return card.replacingAttention(SessionAttention(pending: false,
                error: previous.error, unknown: previous.unknown,
                oldestAt: previous.unknown > 0 ? previous.oldestAt : nil))
        }

        var updatedAttention = attentionCounts
        if let aggregate = attentionCounts, aggregate.isValid, aggregate.pending > 0,
           aggregate.pending >= aggregate.waitingUser, aggregate.pending >= aggregate.waitingApproval,
           aggregate.waitingUser >= (attention.waitingUser > 0 ? 1 : 0),
           aggregate.waitingApproval >= (attention.waitingApproval > 0 ? 1 : 0) {
            let user = aggregate.waitingUser - (attention.waitingUser > 0 ? 1 : 0)
            let approval = aggregate.waitingApproval - (attention.waitingApproval > 0 ? 1 : 0)
            if aggregate.pending - 1 >= max(user, approval) {
                updatedAttention = AttentionCounts(pending: aggregate.pending - 1, waitingUser: user,
                    waitingApproval: approval, error: aggregate.error, unknown: aggregate.unknown)
            }
        }
        var updatedInbox = inboxCounts
        if let aggregate = inboxCounts, aggregate.isValid, aggregate.pending > 0, aggregate.actionRequired > 0,
           aggregate.pending >= aggregate.actionRequired, aggregate.pending >= aggregate.unreadCompleted {
            // A root can also have an independent unread completion. Preserve the
            // union when that membership is true or unknown, and preserve full
            // server totals rather than recounting this truncated card list.
            let pending = root.unreadCompleted == false
                ? max(aggregate.unreadCompleted, max(aggregate.actionRequired - 1, aggregate.pending - 1))
                : aggregate.pending
            updatedInbox = InboxCounts(pending: pending, actionRequired: aggregate.actionRequired - 1,
                unreadCompleted: aggregate.unreadCompleted, unknown: aggregate.unknown)
        }
        return Snapshot(demo: demo, now: now, counts: counts, childrenCounts: childrenCounts,
            sessions: sessions.map(clear), truncated: truncated, date: date, timezone: timezone,
            attentionCounts: updatedAttention, recentCompleted: recentCompleted.map { $0.map(clear) }, inboxCounts: updatedInbox)
    }

    /// Project a successful server acknowledgment into the current snapshot. This
    /// changes only a known root version, without rebuilding totals from cards.
    public func applyingCompletionRead(id: String, through: TimeInterval) -> Snapshot {
        guard through.isFinite, through >= 0 else { return self }
        let recent = recentCompleted ?? []
        let current = sessions.first { $0.id == id }
        guard let root = current ?? recent.first(where: { $0.id == id }), root.role == "main",
              root.unreadCompleted == true, let version = root.inbox?.completedAt, version <= through else { return self }
        let knownVersions = (sessions + recent).filter { $0.id == id }.compactMap { $0.inbox?.completedAt }
        guard !knownVersions.contains(where: { !$0.isFinite || $0 > through }) else { return self }

        let updatedSessions = sessions.map { card -> SessionCard in
            guard card.id == id, card.role == "main", card.unreadCompleted == true,
                  let completedAt = card.inbox?.completedAt, completedAt <= through else { return card }
            return card.replacingInbox(SessionInbox(unreadCompleted: false, completedAt: completedAt))
        }
        let updatedRecent = recentCompleted.map { rows in rows.filter { card in
            !(card.id == id && card.role == "main" && card.unreadCompleted == true &&
              (card.inbox?.completedAt.map { $0 <= through } ?? false))
        } }
        var updatedCounts = inboxCounts
        if let counts = inboxCounts, counts.isValid, counts.pending > 0, counts.unreadCompleted > 0,
           counts.pending >= counts.actionRequired, counts.pending >= counts.unreadCompleted,
           let needsHuman = root.requiresHumanAttention {
            // A human request survives a completion read. Never reduce the root
            // union below its separately reported human-action lower bound.
            let pending = needsHuman ? counts.pending : max(counts.actionRequired, counts.pending - 1)
            updatedCounts = InboxCounts(pending: pending, actionRequired: counts.actionRequired,
                                        unreadCompleted: counts.unreadCompleted - 1, unknown: counts.unknown)
        }
        return Snapshot(demo: demo, now: now, counts: counts, childrenCounts: childrenCounts,
                        sessions: updatedSessions, truncated: truncated, date: date, timezone: timezone,
                        attentionCounts: attentionCounts, recentCompleted: updatedRecent, inboxCounts: updatedCounts)
    }
}
