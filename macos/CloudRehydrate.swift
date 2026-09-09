import Foundation
import Darwin

// This helper uses iCloud's local-cache API only. It never calls removeItem,
// writes cloud objects, reads backup contents, or handles a recovery key.
final class Report: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    private var emitted = false
    private var value: [String: Any] = [
        "state": "unknown", "files": 0, "uploaded": 0, "evicted": 0, "downloaded": 0,
        "evictionRequested": false, "evictionVerified": false, "downloadRequested": false,
        "contentVerified": false, "secondDeviceVerified": false,
        "metadataTimestampChanges": 0,
        "evidence": "iCloud local-cache eviction and download metadata; content verification belongs to the caller"
    ]
    func update(_ fields: [String: Any]) { lock.lock(); defer { lock.unlock() }; value.merge(fields) { _, new in new } }
    func cancel() { lock.lock(); cancelled = true; lock.unlock() }
    func isCancelled() -> Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
    func emit(error: String? = nil) {
        lock.lock()
        if emitted { lock.unlock(); return }
        emitted = true
        if let error { value["state"] = "unknown"; value["error"] = String(error.prefix(500)) }
        value["finishedAt"] = ISO8601DateFormatter().string(from: Date())
        let snapshot = value
        lock.unlock()
        if let data = try? JSONSerialization.data(withJSONObject: snapshot, options: [.sortedKeys]) {
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        }
    }
}

struct Evidence {
    let url: URL
    let relative: String
    let size: Int
    let modified: Date
}

struct Stop: Error { let message: String }
let report = Report()
let manager = FileManager.default
let beginning = ProcessInfo.processInfo.systemUptime
let deadline = beginning + 120
let keys: Set<URLResourceKey> = [.isRegularFileKey, .isDirectoryKey, .isSymbolicLinkKey,
    .isUbiquitousItemKey, .ubiquitousItemIsUploadedKey, .ubiquitousItemIsUploadingKey,
    .ubiquitousItemUploadingErrorKey, .ubiquitousItemDownloadingErrorKey,
    .ubiquitousItemDownloadingStatusKey, .ubiquitousItemIsDownloadingKey,
    .fileSizeKey, .contentModificationDateKey]

report.update(["startedAt": ISO8601DateFormatter().string(from: Date())])
DispatchQueue.global(qos: .utility).async {
    var byte: UInt8 = 0
    while Darwin.read(STDIN_FILENO, &byte, 1) > 0 {}
    report.cancel()
}
// Bounds even a stalled synchronous Foundation call; the caller also owns the
// process group and should treat an unknown/absent receipt as no proof.
DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 120) {
    report.emit(error: "The 120-second cloud recovery deadline expired. No complete recovery proof was recorded.")
    Darwin.exit(124)
}

func checkDeadline() throws {
    if report.isCancelled() { throw Stop(message: "The owning process closed. Cloud files were not deleted.") }
    if ProcessInfo.processInfo.systemUptime >= deadline { throw Stop(message: "The cloud recovery deadline expired.") }
}

func freshValues(_ input: URL) throws -> URLResourceValues {
    var url = input
    url.removeAllCachedResourceValues()
    return try url.resourceValues(forKeys: keys)
}

func rejectLinks(_ input: URL) throws {
    var ancestor = input
    while ancestor.path != "/" {
        try checkDeadline()
        if try freshValues(ancestor).isSymbolicLink == true {
            throw Stop(message: "Symbolic links cannot be used for cloud rehydration.")
        }
        ancestor.deleteLastPathComponent()
    }
}

func inventory(_ folder: URL) throws -> [Evidence] {
    try rejectLinks(folder)
    let folderValues = try freshValues(folder)
    guard folderValues.isDirectory == true else { throw Stop(message: "Choose an existing .ufbackup or .ufvault directory.") }
    guard folderValues.isUbiquitousItem ?? manager.isUbiquitousItem(at: folder) else {
        throw Stop(message: "This directory is not an iCloud item. No local cache was evicted.")
    }
    var enumerationError: Error?
    guard let enumerator = manager.enumerator(at: folder, includingPropertiesForKeys: Array(keys), options: [], errorHandler: { _, error in
        enumerationError = error; return false
    }) else { throw Stop(message: "The backup inventory could not be read.") }
    var files: [Evidence] = []
    var entries = 0
    for case let item as URL in enumerator {
        try checkDeadline()
        entries += 1
        if entries > 20000 { throw Stop(message: "The backup inventory exceeds 20,000 directory entries.") }
        let values = try freshValues(item)
        if values.isSymbolicLink == true { throw Stop(message: "The backup contains a symbolic link. No cloud objects will be changed.") }
        if values.isDirectory == true { continue }
        guard values.isRegularFile == true else { throw Stop(message: "The backup contains an unsupported file type.") }
        guard files.count < 10000 else { throw Stop(message: "The backup exceeds the 10,000-file rehydration limit.") }
        guard values.isUbiquitousItem ?? manager.isUbiquitousItem(at: item),
              values.ubiquitousItemIsUploaded == true, values.ubiquitousItemIsUploading != true,
              values.ubiquitousItemUploadingError == nil, values.ubiquitousItemDownloadingError == nil else {
            throw Stop(message: "Every backup file must have confirmed iCloud upload status before local-cache eviction.")
        }
        guard let size = values.fileSize, let modified = values.contentModificationDate else {
            throw Stop(message: "File identity metadata is unavailable. No verified inventory can be recorded.")
        }
        guard item.path.hasPrefix(folder.path + "/") else { throw Stop(message: "The inventory escaped the selected backup directory.") }
        files.append(Evidence(url: item, relative: String(item.path.dropFirst(folder.path.count + 1)), size: size, modified: modified))
    }
    if let error = enumerationError { throw error }
    guard !files.isEmpty else { throw Stop(message: "The backup directory contains no regular files.") }
    return files.sorted { $0.relative < $1.relative }
}

func sameInventory(_ first: [Evidence], _ second: [Evidence], requireModified: Bool = true) -> Bool {
    guard first.count == second.count else { return false }
    return zip(first, second).allSatisfy { a, b in
        a.relative == b.relative && a.size == b.size && (!requireModified || a.modified == b.modified)
    }
}

func timestampChanges(_ first: [Evidence], _ second: [Evidence]) -> Int {
    zip(first, second).filter { $0.modified != $1.modified }.count
}

func waitForCloudMetadata() { Thread.sleep(forTimeInterval: 0.25) }

func execute() throws {
    guard CommandLine.arguments.count == 2, CommandLine.arguments[1].hasPrefix("/") else {
        throw Stop(message: "Pass one absolute .ufbackup or .ufvault directory path.")
    }
    let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
    guard ["ufbackup", "ufvault"].contains(folder.pathExtension) else {
        throw Stop(message: "Only .ufbackup and .ufvault directories are supported.")
    }
    let files = try inventory(folder)
    report.update(["files": files.count, "uploaded": files.count, "phase": "Upload preflight verified"])
    guard sameInventory(files, try inventory(folder)) else {
        throw Stop(message: "The backup changed during upload preflight. No cache eviction was requested.")
    }
    try checkDeadline()
    report.update(["evictionRequested": true, "phase": "Requesting local cache eviction"])
    // Apple's API removes only the local representation, not the iCloud item.
    // Do not wrap this call in an NSFileCoordinator coordinated write.
    try manager.evictUbiquitousItem(at: folder)
    let evictionDeadline = min(deadline, ProcessInfo.processInfo.systemUptime + 25)
    var evicted = 0
    repeat {
        try checkDeadline()
        evicted = 0
        for file in files {
            try checkDeadline()
            let values = try freshValues(file.url)
            if values.isSymbolicLink == true { throw Stop(message: "A backup path changed into a symbolic link after eviction.") }
            if values.ubiquitousItemDownloadingStatus == .notDownloaded { evicted += 1 }
        }
        report.update(["evicted": evicted])
        if evicted == files.count { break }
        waitForCloudMetadata()
    } while ProcessInfo.processInfo.systemUptime < evictionDeadline
    guard evicted == files.count else {
        throw Stop(message: "macOS did not report every file as not downloaded. Eviction is unproven, so no cloud-recovery success is claimed.")
    }
    report.update(["evictionVerified": true, "downloadRequested": true, "phase": "Requesting the iCloud copy"])
    try checkDeadline()
    try manager.startDownloadingUbiquitousItem(at: folder)
    for file in files {
        try checkDeadline()
        try manager.startDownloadingUbiquitousItem(at: file.url)
    }
    var downloaded = 0
    repeat {
        try checkDeadline()
        downloaded = 0
        for file in files {
            try checkDeadline()
            let values = try freshValues(file.url)
            if values.isSymbolicLink == true { throw Stop(message: "A backup path changed into a symbolic link during download.") }
            if let error = values.ubiquitousItemDownloadingError { throw error }
            if values.ubiquitousItemDownloadingStatus == .current && values.ubiquitousItemIsDownloading != true {
                downloaded += 1
            }
        }
        report.update(["downloaded": downloaded])
        if downloaded == files.count { break }
        waitForCloudMetadata()
    } while true
    let downloadedFiles = try inventory(folder)
    guard sameInventory(files, downloadedFiles, requireModified: false) else {
        throw Stop(message: "The downloaded inventory differs from the verified upload inventory. The caller must not treat it as the same backup.")
    }
    // iCloud may normalize modification times while downloading unchanged bytes.
    // Paths and sizes are necessary evidence, not content integrity: the caller
    // must still verify hashes and decrypt before claiming recovery succeeded.
    report.update(["state": "rehydrated", "phase": "Local eviction and iCloud download observed",
                   "metadataTimestampChanges": timestampChanges(files, downloadedFiles),
                   "elapsedSeconds": ProcessInfo.processInfo.systemUptime - beginning])
}

do {
    try execute()
    report.emit()
} catch let error as Stop {
    report.emit(error: error.message)
} catch {
    report.emit(error: error.localizedDescription)
}
