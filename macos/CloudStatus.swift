import Foundation

// Read-only operating-system metadata. An upload flag is not cloud readback.
var result: [String: Any] = ["state": "unknown", "files": 0, "uploaded": 0,
                            "evidence": "macOS iCloud upload metadata; no remote readback"]
let manager = FileManager.default
let keys: Set<URLResourceKey> = [.isRegularFileKey, .isDirectoryKey, .isSymbolicLinkKey,
                                .isUbiquitousItemKey, .ubiquitousItemIsUploadedKey,
                                .ubiquitousItemIsUploadingKey, .ubiquitousItemUploadingErrorKey]
func inspect() throws {
    guard CommandLine.arguments.count == 2, CommandLine.arguments[1].hasPrefix("/") else {
        result["error"] = "Pass the absolute path of one .ufbackup or .ufvault folder."
        return
    }
    let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
    guard ["ufbackup", "ufvault"].contains(folder.pathExtension) else {
        result["error"] = "Choose a .ufbackup or .ufvault folder."
        return
    }
    // Refuse symlinks anywhere in the supplied path, including intermediate folders.
    var ancestor = folder
    while ancestor.path != "/" {
        if try ancestor.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink == true {
            result["error"] = "Symbolic links are not inspected."
            return
        }
        ancestor.deleteLastPathComponent()
    }
    let folderValues = try folder.resourceValues(forKeys: keys)
    guard folderValues.isDirectory == true else {
        result["error"] = "The backup folder does not exist."
        return
    }
    var traversalFailed = false
    var lastError: String?
    guard let enumerator = manager.enumerator(at: folder, includingPropertiesForKeys: Array(keys),
                                             options: [], errorHandler: { _, error in
        traversalFailed = true
        lastError = String(error.localizedDescription.prefix(400))
        return false
    }) else {
        result["error"] = "The backup folder could not be inspected."
        return
    }
    var files = 0
    var uploaded = 0
    var cloudFiles = 0
    var nonCloudFiles = 0
    var visited = 0
    for case let file as URL in enumerator {
        visited += 1
        if visited > 10000 {
            traversalFailed = true
            lastError = "The metadata inspection limit of 10,000 entries was reached."
            break
        }
        let values = try file.resourceValues(forKeys: keys)
        if values.isSymbolicLink == true {
            traversalFailed = true
            lastError = "A symbolic link was found inside the backup folder."
            enumerator.skipDescendants()
            continue
        }
        if values.isDirectory == true { continue }
        guard values.isRegularFile == true else {
            traversalFailed = true
            lastError = "The backup folder contains an unsupported file type."
            continue
        }
        files += 1
        if let error = values.ubiquitousItemUploadingError {
            lastError = String(error.localizedDescription.prefix(400))
        }
        if values.isUbiquitousItem ?? manager.isUbiquitousItem(at: file) {
            cloudFiles += 1
            if values.ubiquitousItemIsUploaded == true && values.ubiquitousItemIsUploading != true && values.ubiquitousItemUploadingError == nil {
                uploaded += 1
            }
        } else {
            nonCloudFiles += 1
        }
        result["files"] = files
        result["uploaded"] = uploaded
    }
    if traversalFailed || files == 0 || (cloudFiles > 0 && nonCloudFiles > 0) {
        result["state"] = "unknown"
    } else if nonCloudFiles == files {
        result["state"] = (folderValues.isUbiquitousItem ?? manager.isUbiquitousItem(at: folder)) ? "upload_pending" : "not_icloud"
    } else if cloudFiles == files && uploaded == files && lastError == nil {
        result["state"] = "uploaded"
    } else {
        result["state"] = "upload_pending"
    }
    if let error = lastError { result["error"] = error }
}

do {
    try inspect()
} catch {
    result["state"] = "unknown"
    result["error"] = String(error.localizedDescription.prefix(400))
}
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
