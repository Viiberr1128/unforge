import Foundation
import CoreServices
import Darwin

// No filenames leave this process. The owner receives a bounded change signal.
let arguments = CommandLine.arguments
guard arguments.count == 2, arguments[1].hasPrefix("/") else { exit(2) }
let folder = URL(fileURLWithPath: arguments[1], isDirectory: true).standardizedFileURL
let canonical = folder.resolvingSymlinksInPath()
var isDirectory: ObjCBool = false
guard canonical.path == folder.path,
      FileManager.default.fileExists(atPath: folder.path, isDirectory: &isDirectory),
      isDirectory.boolValue else { exit(2) }
let watchRoot = folder.path
let ignoredComponents: Set<String> = [".backups", ".runtime", "operation-owners", ".server.lock"]
var lastSignal: TimeInterval = 0
var pendingSignal: DispatchWorkItem?

func signalChange() {
    // One batch produces one small line, with a maximum two signals per second.
    let now = ProcessInfo.processInfo.systemUptime
    if now - lastSignal >= 0.5 {
        lastSignal = now
        pendingSignal?.cancel()
        pendingSignal = nil
        FileHandle.standardOutput.write(Data("changed\n".utf8))
    } else if pendingSignal == nil {
        let work = DispatchWorkItem {
            pendingSignal = nil
            signalChange()
        }
        pendingSignal = work
        DispatchQueue.main.asyncAfter(deadline: .now() + (0.5 - (now - lastSignal)), execute: work)
    }
}

let callback: FSEventStreamCallback = { _, _, count, rawPaths, flags, _ in
    let paths = unsafeBitCast(rawPaths, to: NSArray.self)
    for index in 0..<min(count, paths.count) {
        guard let rawPath = paths[index] as? String else { continue }
        let eventURL = URL(fileURLWithPath: rawPath).standardizedFileURL
        let path = eventURL.deletingLastPathComponent().resolvingSymlinksInPath().appendingPathComponent(eventURL.lastPathComponent).path
        // Dropped events require a conservative change signal; there is no scan.
        let uncertain = UInt32(kFSEventStreamEventFlagMustScanSubDirs | kFSEventStreamEventFlagUserDropped | kFSEventStreamEventFlagKernelDropped | kFSEventStreamEventFlagRootChanged)
        if flags[index] & UInt32(kFSEventStreamEventFlagRootChanged) != 0 && !FileManager.default.fileExists(atPath: watchRoot) {
            FileHandle.standardOutput.write(Data("failed\n".utf8))
            exit(4)
        }
        if flags[index] & uncertain != 0 { signalChange(); return }
        guard path == watchRoot || path.hasPrefix(watchRoot + "/") else { continue }
        let relative = path == watchRoot ? "" : String(path.dropFirst(watchRoot.count + 1))
        if relative.split(separator: "/").contains(where: { ignoredComponents.contains(String($0)) }) { continue }
        signalChange()
        return
    }
}
var context = FSEventStreamContext(version: 0, info: nil, retain: nil, release: nil, copyDescription: nil)
let flags = FSEventStreamCreateFlags(kFSEventStreamCreateFlagUseCFTypes | kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagWatchRoot)
guard let stream = FSEventStreamCreate(nil, callback, &context, [watchRoot] as CFArray,
                                     FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 0.5, flags) else { exit(3) }
FSEventStreamSetDispatchQueue(stream, DispatchQueue.main)
guard FSEventStreamStart(stream) else {
    FSEventStreamInvalidate(stream)
    FSEventStreamRelease(stream)
    exit(3)
}
// The engine owns this stdin pipe. Abrupt engine death closes it, too.
DispatchQueue.global(qos: .utility).async {
    var byte: UInt8 = 0
    while Darwin.read(STDIN_FILENO, &byte, 1) > 0 {}
    DispatchQueue.main.async {
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
        exit(0)
    }
}
FileHandle.standardOutput.write(Data("ready\n".utf8))
dispatchMain()
