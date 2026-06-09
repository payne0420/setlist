# Homebrew Cask for Setlist
# Install:
#   brew tap payne0420/setlist https://github.com/payne0420/setlist
#   brew install --cask setlist

cask "setlist" do
  version "2.1.0"
  sha256 "28a4ca5406300ca41fbed4e3eee67af7833e1b2a834fc1738dc03ae240564c3e"

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
