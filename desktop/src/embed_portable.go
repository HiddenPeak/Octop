//go:build !production || darwin

package main

// Development builds look for inhere-box-<plat>.zip or inhere-box-portable-<plat>-*.zip
// beside the executable. macOS production copies the zip into Resources as
// inhere-box-<plat>.zip.
var embeddedPortable []byte
