# Homebrew Cask for Setlist
# Install:
#   brew tap payne0420/setlist https://github.com/payne0420/setlist
#   brew install --cask setlist

cask "setlist" do
  version "2.2.0"
  sha256 "616a00f776b4e3a3ea7ffac3a6db60f48a03be3f4371eba0873dc2a973eefe20"

  url "https://github.com/payne0420/setlist/releases/download/v#{version}/Setlist-macOS.zip"
  name "Setlist"
  desc "Download Spotify playlists to local MP3s with artwork and tags"
  homepage "https://github.com/payne0420/setlist"

  app "Setlist.app"

  uninstall quit: "com.sunnypatel.setlist"

  zap trash: [
    "~/Library/Application Support/Setlist",
    "~/Library/Preferences/com.sunnypatel.setlist.plist",
    "~/Library/Caches/com.sunnypatel.setlist",
  ]

  caveats <<~EOS
    FFmpeg is bundled with the app - no separate installation needed.

    After installation, run this command to remove macOS quarantine:
      sudo xattr -cr /Applications/Setlist.app

    Educational use only. Ensure compliance with copyright laws.
  EOS
end
