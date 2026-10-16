import Foundation

/// Errors intentionally contain no response bodies, cookies, or CSRF values.
public enum APIClientError: Error, Equatable, LocalizedError {
    case invalidBaseURL
    case invalidPath
    case closed
    case invalidResponse
    case bootstrapMissingCookie
    case bootstrapMissingCSRF
    case redirectRejected
    case httpStatus(Int)
    case decodingFailed
    case network(URLError.Code)

    public var errorDescription: String? {
        switch self {
        case .invalidBaseURL: return "服务地址须为带有效端口的 http://127.0.0.1"
        case .invalidPath: return "不支持此接口"
        case .closed: return "连接已关闭"
        case .invalidResponse: return "服务响应无效"
        case .bootstrapMissingCookie, .bootstrapMissingCSRF: return "首页认证字段不完整"
        case .redirectRejected: return "已拒绝服务重定向"
        case .httpStatus(let status): return "服务返回错误（\(status)）"
        case .decodingFailed: return "服务数据格式不兼容"
        case .network(.timedOut): return "连接超时，操作未自动重试"
        case .network: return "暂时无法连接本机服务"
        }
    }
}

/// A connection owns its credentials and never uses the system's cookie store.
/// Create a new client when changing origin, and close the old client first.
public actor APIClient {
    public nonisolated let baseURL: URL
    private let session: URLSession
    private let delegate: LocalSessionDelegate
    private var credentials: Credentials?
    private var bootstrap: Bootstrap?
    private var isClosed = false
    private var lifecycle: UInt64 = 0

    private struct Credentials: Sendable {
        let id = UUID()
        let cookie: String
        let csrf: String
    }

    private struct Bootstrap {
        let id: UUID
        let lifecycle: UInt64
        let task: Task<Credentials, Error>
    }

    public init(baseURL: URL) throws {
        self.baseURL = try Self.validatedOrigin(baseURL)
        let delegate = LocalSessionDelegate()
        self.delegate = delegate
        self.session = Self.makeSession(configuration: .ephemeral, delegate: delegate)
    }

    /// URLProtocol injection is available only to this module's tests.
    internal init(baseURL: URL, configuration: URLSessionConfiguration) throws {
        self.baseURL = try Self.validatedOrigin(baseURL)
        let delegate = LocalSessionDelegate()
        self.delegate = delegate
        self.session = Self.makeSession(configuration: configuration, delegate: delegate)
    }

    public func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        let url = try endpoint(path, method: "GET")
        let first = try await authenticated()
        let firstResponse = try await send(url, method: "GET", credentials: first)
        if firstResponse.statusCode == 401 {
            // Only invalidate the authentication that this response actually used.
            // A delayed 401 must never remove a newer successful bootstrap.
            if credentials?.id == first.id { credentials = nil }
            let replacement = try await authenticated()
            let retry = try await send(url, method: "GET", credentials: replacement)
            return try decode(retry, as: type)
        }
        return try decode(firstResponse, as: type)
    }

    public func post<T: Decodable>(_ path: String, body: [String: String] = [:], as type: T.Type) async throws -> T {
        let url = try endpoint(path, method: "POST")
        let authentication = try await authenticated()
        let payload = try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
        let response = try await send(url, method: "POST", credentials: authentication, body: payload)
        if response.statusCode == 401, credentials?.id == authentication.id {
            credentials = nil
        }
        // Never replay an action, including after 401, timeout, or an ambiguous result.
        return try decode(response, as: type)
    }

    public func close() {
        guard !isClosed else { return }
        isClosed = true
        lifecycle &+= 1
        credentials = nil
        bootstrap?.task.cancel()
        bootstrap = nil
        session.invalidateAndCancel()
    }

    private static func makeSession(configuration input: URLSessionConfiguration, delegate: LocalSessionDelegate) -> URLSession {
        let configuration = input.copy() as! URLSessionConfiguration
        configuration.httpShouldSetCookies = false
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 6
        configuration.timeoutIntervalForResource = 6
        configuration.httpAdditionalHeaders = nil
        configuration.httpShouldUsePipelining = false
        return URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
    }

    private static func validatedOrigin(_ url: URL) throws -> URL {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme == "http", parts.host == "127.0.0.1",
              let port = parts.port, (1...65_535).contains(port),
              parts.user == nil, parts.password == nil, parts.path.isEmpty,
              parts.query == nil, parts.fragment == nil else {
            throw APIClientError.invalidBaseURL
        }
        var origin = URLComponents()
        origin.scheme = "http"
        origin.host = "127.0.0.1"
        origin.port = port
        guard let result = origin.url else { throw APIClientError.invalidBaseURL }
        return result
    }

    private func endpoint(_ path: String, method: String) throws -> URL {
        try checkOpen()
        guard path.hasPrefix("/"), !path.hasPrefix("//"), !path.contains("\\"),
              !path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              let relative = URLComponents(string: path),
              relative.scheme == nil, relative.host == nil, relative.user == nil,
              relative.password == nil, relative.fragment == nil else {
            throw APIClientError.invalidPath
        }
        let allowed = method == "GET"
            ? ["/api/snapshot", "/api/subscriptions", "/api/children", "/api/integrations"]
            : ["/api/jump", "/api/rescan", "/api/inbox/read", "/api/attention/dismiss"]
        guard allowed.contains(relative.path), method != "POST" || relative.query == nil else {
            throw APIClientError.invalidPath
        }
        var result = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        result.path = relative.path
        // Decode and re-encode query items rather than resolving a relative URL.
        result.queryItems = relative.queryItems
        guard let url = result.url else { throw APIClientError.invalidPath }
        return url
    }

    private func checkOpen(_ expectedLifecycle: UInt64? = nil) throws {
        guard !isClosed, expectedLifecycle == nil || expectedLifecycle == lifecycle else {
            throw APIClientError.closed
        }
        try Task.checkCancellation()
    }

    private func authenticated() async throws -> Credentials {
        try checkOpen()
        if let credentials { return credentials }
        let flight: Bootstrap
        if let bootstrap {
            flight = bootstrap
        } else {
            var home = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
            home.path = "/"
            let url = home.url!
            let session = self.session
            let origin = self.baseURL
            flight = Bootstrap(id: UUID(), lifecycle: lifecycle, task: Task {
                let response = try await Self.perform(Self.request(url), session: session, origin: origin)
                guard (200..<300).contains(response.statusCode) else {
                    throw APIClientError.httpStatus(response.statusCode)
                }
                return try Self.authentication(from: response)
            })
            bootstrap = flight
        }
        do {
            let result = try await flight.task.value
            guard !isClosed, flight.lifecycle == lifecycle else { throw APIClientError.closed }
            if bootstrap?.id == flight.id {
                credentials = result
                bootstrap = nil
            }
            // The flight belongs to the connection. A cancelled waiter may not
            // discard its successful result before other waiters can adopt it.
            try Task.checkCancellation()
            return result
        } catch {
            if bootstrap?.id == flight.id { bootstrap = nil }
            try checkOpen(flight.lifecycle)
            throw error
        }
    }

    private struct Response {
        let data: Data
        let http: HTTPURLResponse
        var statusCode: Int { http.statusCode }
    }

    private static func request(_ url: URL) -> URLRequest {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 6)
        request.httpShouldHandleCookies = false
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        return request
    }

    private func send(_ url: URL, method: String, credentials: Credentials, body: Data? = nil) async throws -> Response {
        try checkOpen()
        let startedLifecycle = lifecycle
        var request = Self.request(url)
        request.httpMethod = method
        request.setValue("agentwatch=\(credentials.cookie)", forHTTPHeaderField: "Cookie")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if method == "POST" {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue(credentials.csrf, forHTTPHeaderField: "X-AgentWatch-CSRF")
            request.httpBody = body
        }
        do {
            let response = try await Self.perform(request, session: session, origin: baseURL)
            try checkOpen(startedLifecycle)
            return response
        } catch {
            try checkOpen(startedLifecycle)
            throw error
        }
    }

    private static func perform(_ request: URLRequest, session: URLSession, origin: URL) async throws -> Response {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse,
                  let url = http.url,
                  url.scheme == origin.scheme, url.host == origin.host, url.port == origin.port else {
                throw APIClientError.invalidResponse
            }
            if (300..<400).contains(http.statusCode) { throw APIClientError.redirectRejected }
            return Response(data: data, http: http)
        } catch let error as APIClientError {
            throw error
        } catch let error as URLError {
            if Task.isCancelled { throw CancellationError() }
            throw APIClientError.network(error.code)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw APIClientError.invalidResponse
        }
    }

    private static func authentication(from response: Response) throws -> Credentials {
        let headers = response.http.allHeaderFields.reduce(into: [String: String]()) { result, pair in
            if let key = pair.key as? String, let value = pair.value as? String { result[key] = value }
        }
        let cookies = HTTPCookie.cookies(withResponseHeaderFields: headers, for: response.http.url!)
            .filter { $0.name == "agentwatch" && $0.domain == "127.0.0.1" && $0.path == "/" && !$0.isSecure }
        guard cookies.count == 1, let cookie = cookies.first, validToken(cookie.value),
              cookie.expiresDate.map({ $0 > Date() }) ?? true else {
            throw APIClientError.bootstrapMissingCookie
        }
        guard response.http.mimeType == "text/html", response.data.count <= 1_048_576,
              let html = String(data: response.data, encoding: .utf8),
              let csrf = csrfMeta(in: html), validToken(csrf) else {
            throw APIClientError.bootstrapMissingCSRF
        }
        return Credentials(cookie: cookie.value, csrf: csrf)
    }

    private static func validToken(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 4096 && value.unicodeScalars.allSatisfy {
            CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-").contains($0)
        }
    }

    private static func csrfMeta(in html: String) -> String? {
        let tags = try! NSRegularExpression(pattern: "<meta\\b[^>]*>", options: [.caseInsensitive])
        let attributes = try! NSRegularExpression(
            pattern: #"([A-Za-z_:][A-Za-z0-9_:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))"#)
        var found: [String] = []
        let fullRange = NSRange(html.startIndex..., in: html)
        for tag in tags.matches(in: html, range: fullRange) {
            guard let range = Range(tag.range, in: html) else { continue }
            let text = String(html[range])
            var values: [String: String] = [:]
            for attribute in attributes.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
                guard let nameRange = Range(attribute.range(at: 1), in: text) else { continue }
                let name = text[nameRange].lowercased()
                for index in 2...4 {
                    if let valueRange = Range(attribute.range(at: index), in: text) {
                        values[name] = String(text[valueRange])
                        break
                    }
                }
            }
            if values["name"]?.lowercased() == "csrf", let value = values["content"] { found.append(value) }
        }
        return found.count == 1 ? found[0] : nil
    }

    private func decode<T: Decodable>(_ response: Response, as type: T.Type) throws -> T {
        guard (200..<300).contains(response.statusCode) else {
            throw APIClientError.httpStatus(response.statusCode)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do { return try decoder.decode(type, from: response.data) }
        catch { throw APIClientError.decodingFailed }
    }
}

private final class LocalSessionDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        completionHandler(.cancelAuthenticationChallenge, nil)
    }
}
