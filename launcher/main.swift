// Spicetify Updater launcher.
//
// macOS only lets apps holding the App Management permission write inside
// other apps' bundles, and a LaunchAgent running python directly is refused.
// This tiny app is what the LaunchAgent starts, so the permission goes to one
// named app instead of to python. It runs updater.py, which inherits the grant.
//
// Rebuilding this binary changes its signature, and macOS then forgets the
// App Management grant. install.sh only rebuilds when this file or
// Info.plist changes.

import Foundation

let script = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/Spicetify Updater/updater.py").path
let args = Array(CommandLine.arguments.dropFirst())

// Opened from Finder: nothing to do.
if args.isEmpty { exit(0) }

var childPID: pid_t = 0
for sig in [SIGTERM, SIGINT] {
    signal(sig) { received in
        if childPID > 0 { kill(childPID, received) }
    }
}

let child = Process()
child.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
child.arguments = ["-I", script] + args
do {
    try child.run()
} catch {
    FileHandle.standardError.write("couldn't start updater.py: \(error)\n".data(using: .utf8)!)
    exit(1)
}
childPID = child.processIdentifier
child.waitUntilExit()
exit(child.terminationStatus)
