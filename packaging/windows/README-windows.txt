ClearJAV Uploader for Windows
=============================

Before the first start: install FFmpeg and MediaInfo
----------------------------------------------------
The uploader needs them to read the videos and make the contact sheets. They are NOT
included in this download: get them from their own sites (always the current version).

- FFmpeg: https://www.gyan.dev/ffmpeg/builds/
  Take the "release essentials" build (zip). ffmpeg.exe and ffprobe.exe are in its bin folder.
  (FFmpeg for other systems: https://ffmpeg.org/download.html)
- MediaInfo, the command-line version (CLI): https://mediaarea.net/en/MediaInfo/Download
  Choose Windows, then the "CLI" download (zip). It holds MediaInfo.exe.

Put ffmpeg.exe, ffprobe.exe and MediaInfo.exe next to ClearJAV-Uploader.exe (or in a folder
named "bin" there). Or keep them anywhere on PATH, or give their places on the page:
Settings -> Programs -> "Use other copies". Then "Check setup again".

Start
-----
Start ClearJAV-Uploader.exe. A window opens and your browser shows the uploader's page
(only this computer can use it). Keep the window open while you upload; closing it stops
the uploader.

First time, in the page's Settings tab:
1. API key: clearjav.com -> your profile -> Settings -> API Key. Paste it, "Save and check".
2. Announce URL (optional): clearjav.com -> Upload page -> "Announce URL". Paste it, "Save".
3. Seeding: only save the .torrent, put it in a watch folder, or add it to qBittorrent.

The key, the announce URL and the qBittorrent password are kept in Windows Credential
Manager, never in a file. The log (without them) is in %LOCALAPPDATA%\ClearJAV-AIO\Logs,
the settings in %APPDATA%\ClearJAV-AIO\config.ini.

Then, on the Upload tab: paste a folder or use Browse, answer what the page asks, Start.

The page is dark or light as Windows is set; the button at the top right switches between
ClearJAV Dark and Light. The list shows the videos' covers: DMM's pictures, which
the uploader loads from DMM's image server. Settings -> Page switches them off.

Terminal use: ClearJAV-Uploader.exe --help (e.g. ClearJAV-Uploader.exe upload D:\Videos -r).

Windows may warn that the program is from an unknown publisher (it isn't signed):
"More info" -> "Run anyway".

With Windows' Smart App Control on, the unsigned program is blocked completely (there is
no "Run anyway"). Then use the source version instead: run.bat, from the uploader's
source download (it needs Python 3.10 or newer; see its README.md).
