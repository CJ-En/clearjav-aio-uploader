# ClearJAV AIO Uploader

Uploads DMM WEB-DLs and disc Remuxes to ClearJAV in one go:
1. looks the video up on r18.dev;
2. warns when it's already on ClearJAV;
3. makes the MediaInfo text, the contact sheet and the torrent;
4. uploads;
5. saves the torrent you must seed, and can hand it to your torrent client.

It runs as a page in your browser (only your computer can use it), or in the terminal.

- **Titles** in the format of the Torrent Naming Guide, as the site's title checker writes them:
  - WEB-DL: `IPZZ-020 2023-04-07 1080p DMM WEB-DL H.264 AAC`.
  - Remux: `KOBP-07 2008-12-29 480i DVD REMUX MPEG-2 AC-3`, `ABC-123 2010-05-01 1080i Blu-Ray REMUX AVC PCM`.
  - Resolution: `SD` below 404p, `2160p-VR` / `8K-VR` for VR, `i` for interlaced Remuxes.
  - Codecs as used on ClearJAV: `H.264` (`AVC` on Remuxes), `HEVC`, `VP9`, `VC-1`, `MPEG-2`; audio
    `AAC`, `WMA`, `AC-3`, `E-AC-3`, `PCM`, `MP2`, `TrueHD`, `DTS`, `DTS-HD MA`.
  - Tags: `-ClearJAV` for internal releases; a personal release may carry `-YourTag`.
- **File names** as the Upload Rules ask: the video is renamed to its content id (`ipzz00020.mkv`),
  or its DVD id or title if you prefer. Parts become `ipzz00020-pt1.mkv`, `-pt2` in a folder `ipzz00020`.
- **Contact sheets** in mtn's layout (as on the site's own uploads): four lines of file
  information, 3 columns of shots with their times. Made with FFmpeg alone (mtn isn't needed).
- **Torrents** made by the tool itself, with the same piece size as the site's own uploads, so
  ClearJAV recognises a copy of a torrent that is already there.
- **The right resolution** (1080p, 2160p, VR, 8K-VR, ...) and type (WEB-DL or Remux) are chosen for you.

## Changes in 1.0.1

- Contact sheets work with FFmpeg 9.1.
- An upload that waits in the moderation queue says so: "Uploaded: waits for staff approval", on the page and in
  the terminal (the site answers "Torrent uploaded successfully" either way).
- Settings → Programs shows FFmpeg and ffprobe each with its own state: a missing ffprobe no longer shows FFmpeg as
  missing too, and the other way round.
- A video the tool can never upload (an ISO, a part on its own, a rip, video or audio the site doesn't take) no
  longer offers "Check again". For a video refused before it could be checked (its folder held something else, a
  part was missing), "Check again" now reads its folder again.
- Settings: the account panel says what happens to the site's .torrent (the tool saves it, puts it in your watch
  folder or hands it to qBittorrent; it doesn't seed it itself).
- The box for the output folder shows its whole placeholder.
- Windows: with Smart App Control on, Windows blocks the program completely; use `run.bat` (below).

## What you need

Install these yourself. **They are not included**, also not in the Windows download. Always take the
current version from their own sites:

- **FFmpeg** (`ffmpeg` and `ffprobe`): reads the videos and makes the contact sheets.
  - Windows: [gyan.dev](https://www.gyan.dev/ffmpeg/builds/), the "release essentials" build (zip).
    `ffmpeg.exe` and `ffprobe.exe` are in its `bin` folder.
  - All systems: [ffmpeg.org](https://ffmpeg.org/download.html).
  - Debian/Ubuntu: `sudo apt install ffmpeg`.
- **MediaInfo, the command-line version (CLI)**: the MediaInfo text of each video. Not the MediaInfo
  program with windows: the CLI.
  - All systems: [mediaarea.net](https://mediaarea.net/en/MediaInfo/Download) (Windows: the "CLI" zip,
    it holds `MediaInfo.exe`).
  - Debian/Ubuntu: `sudo apt install mediainfo`.
- **Python 3.10 or newer**: only to run the tool from the source (`run.bat`, `run.sh`), not for the
  Windows download.
  - Windows: [python.org](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
  - Debian/Ubuntu: `python3` and `python3-venv`.

**How the tool finds FFmpeg and MediaInfo**, in this order:
1. the place you give it: the page's Settings → Programs → "Use other copies", or `mediainfo`,
   `ffmpeg`, `ffprobe` in the settings file;
2. the tool's own folder (next to `ClearJAV-Uploader.exe`, or next to `run.bat` / `run.sh`), or a
   `bin` folder there;
3. the folders on PATH (where `apt` installs them).

Settings → Programs shows what was found, with the download links; press "Check setup again" after
installing. In the terminal: `run.bat doctor` (Linux: `sh run.sh doctor`).

## Windows: the download

1. Install FFmpeg and MediaInfo CLI (above). The simplest: put `ffmpeg.exe`, `ffprobe.exe` and
   `MediaInfo.exe` next to `ClearJAV-Uploader.exe`.
2. Unzip `ClearJAV-Uploader-<version>-windows-x64.zip` and start `ClearJAV-Uploader.exe`. A window
   opens and your browser shows the page; keep the window open while you upload (closing it stops
   the uploader).

Windows may say the program is from an unknown publisher (it isn't signed): "More info" → "Run anyway".
With Windows' **Smart App Control** on, the unsigned program is blocked completely (there is no "Run
anyway"): then use the source version, `run.bat` (below).

## From the source (Windows, Linux, macOS)

Install Python, FFmpeg and MediaInfo CLI (above). Then Windows: double-click `run.bat`. Linux:
`sh run.sh`. The first start prepares a private Python environment in `.venv` (one minute); later
starts are immediate. Without anything after it, it opens the browser page; with arguments it runs
in the terminal (below).

## First time

You need **a ClearJAV API key**: your profile > Settings > API Key.

On the page: **Settings**
1. **API key**: paste it, "Save and check".
2. **Announce URL** (optional): from the Upload page ("Announce URL"). It goes into every .torrent
   the tool makes, so the file is a normal private ClearJAV torrent from the start. The site still
   makes its own .torrent on upload: that is the one to seed, and the tool saves it, puts it in your
   watch folder or hands it to qBittorrent (Seeding, below).
3. **Seeding**: after the upload the tool downloads the site's .torrent and
   - only saves it (you add it to your client), or
   - copies it into a **watch folder** (the client must save it into the folder of your videos,
     or it downloads the video again instead of seeding it), or
   - adds it to **qBittorrent** through its Web UI (Tools > Options > Web UI), seeding at once from
     the video's own folder.

In the terminal: `run.bat login`, `run.bat announce`, `run.bat qbittorrent`, `run.bat doctor`.

**Where the key, the announce URL and the qBittorrent password are kept.** They hold your passkey, so
they never go into the settings file and are never written to the log.
- Windows: in Windows Credential Manager (entries named `clearjav-aio`). `run.bat` and the Windows
  program share them.
- macOS: in the Keychain.
- Linux (and other systems): in the file `secrets.ini` next to the settings file
  (`~/.config/clearjav-aio/secrets.ini`). Only your account can read it (the tool writes it with mode
  `600`), so it is as safe as your login: root and anyone using your account can read it too. The
  tool doesn't use the desktop's keyring (Secret Service), which is locked or missing on a server
  reached over SSH.

Environment variables always win over saved values: `CLEARJAV_API_KEY`, `CLEARJAV_ANNOUNCE_URL` and
`QBITTORRENT_PASSWORD`. With `CLEARJAV_AIO_NO_KEYRING=1` nothing is saved anywhere: the tool then
reads them only from the environment. On Windows and macOS, a password store that keeps nothing or
keeps secrets in a plain file is not used, and one that doesn't answer within 10 s is left alone for
the rest of the run.

**On a server without a screen** (a seedbox): start `sh run.sh gui --no-browser`; it prints the
page's address. On your computer, run `ssh -L 8765:127.0.0.1:8765 you@server` and open that address.

## Upload (the page)

1. Paste a folder or a video (or use **Browse…**), tick **Subfolders** to look inside them, **Add**.
2. Each video is checked at once: the list shows the title it will get, and asks you where needed:
   - **Which video is this?** when neither the file nor its name tells the content id. A name that
     is a DVD id (`GVH-882.mp4`) is looked up on r18.dev and DMM, and the content id found is filled
     in, marked "suggested from the DVD-ID", with what r18.dev or DMM says about that video (title, maker,
     release date, length). Another maker may use the same DVD id: check it is your video, then use it;
   - **No DVD id**: confirm the DVD id the tool worked out;
   - **Unknown on r18.dev**: possible only when ClearJAV already has the video (a new video needs its
     title and cover, which only the website's upload form takes); confirm the DVD id and date;
   - a video refused because of its content id can be checked again with another one;
   - **Already on ClearJAV**: upload anyway (e.g. better quality) or skip. A torrent with exactly the
     same title is refused (the site takes no second one): only another version can go up (another
     resolution or type, or a personal release with your tag);
   - Remuxes: the **source** (DVD, VHS, Blu-Ray, 3D Blu-Ray, UHD Blu-Ray), worked out from the
     name or size: check it.
3. **Start** uploads the ready videos one after another. **Test run** makes everything without
   uploading, and shows what a real upload would refuse or ask.

Other videos in an added folder (`.avi`, `.mov`, `.ts` ...) are not uploaded; the tool says which.

**Covers**: the list shows each video's cover. They are DMM's pictures: r18.dev gives their address, and the
uploader loads them from DMM's image server (`pics.dmm.co.jp`) and hands them to the page (your browser only talks
to the uploader). Videos r18.dev doesn't know get a tile with their DVD-ID's letters. Settings → Page switches
covers off; then nothing is loaded from DMM's image server.

**Theme**: the page is drawn in ClearJAV's colors, dark or light as your computer is set. The button at the top
right switches between ClearJAV Dark and Light; the choice is saved with the other settings (`theme` in the
settings file; `system` follows the computer again).

## Upload (the terminal)

```
run.bat "D:\Videos\ipzz00020.mkv"
run.bat "D:\Videos" --recursive
run.bat "D:\Videos\abc00123.mkv" --dry-run
```

You can also drag videos or folders onto `run.bat`. Questions are asked in the window; without anyone
to ask (scripts), guessed DVD ids are used with a warning, unknown ids and duplicates are skipped.

**What to upload**
- **The content id must be known**: files decrypted from DMM carry it inside, whatever they are
  called; other files must be named after it (`ipzz00020.mkv`), or you type it. A file named by its
  DVD id (`IPZZ-020.mkv`) gets the content id from r18.dev and DMM as the suggested answer (never
  used without you). The tool then renames them as set (`--rename`).
- **One file per video.** Join H.264 parts first (VideoMerger).
- **4K VP9 videos in parts**: put the parts in a folder of their own (`abc00123-pt1.mkv`,
  `abc00123-pt2.mkv`) and upload the folder. It becomes one torrent with both files. The folder is
  renamed with the parts, so it may hold nothing else (small .nfo/.txt/picture files are fine).
  A single part on its own is refused. The parts must be numbered 1, 2, 3 ... without a gap and be
  alike (WEB-DL or Remux, size, codec, frame rate); parts of different videos each need a folder of
  their own. The folder of a single video plays no role. DMM's own listings with letters after the
  number (`1sddm00972ai`, the AI upscale; `1start00561v`, the extended version) work the same way.
- **DMM WEB-DLs**: `.mkv`, `.mp4` or `.wmv` with H.264, HEVC, VP9 or VC-1 video and AAC or WMA audio.
- **Disc Remuxes** (one file): the disc's own video (AVC, HEVC, VC-1, MPEG-2) and audio (PCM, AC-3,
  E-AC-3, DTS, TrueHD, MP2). Disc audio, disc subtitles or "Remux", "Blu-ray", "DVD", "VHS" in the
  name make a file a Remux. Re-encoded videos and rips are refused; **ISOs** are uploaded on the
  website after staff approve them.

**What happens for each video**
1. **r18.dev** gives the DVD id and release date for the title.
   - Many DMM digital ids (`ipzz00020`) have no DVD id there: it then asks DMM's own product
     API (`IPZZ-020`).
   - Only when DMM has none either is it worked out from the id, and you're asked to confirm.
   - Without a release date on r18.dev, DMM's is used. With none anywhere, the terminal asks for
     it; the page says the title goes without one. FC2 titles never have a date.
   - Ids r18.dev doesn't know at all are uploaded only when ClearJAV already has the video, after
     you confirm DVD id and date (taken from DMM or ClearJAV's torrent); otherwise use the website.
   - r18.dev also finds videos by DVD id: when it answers with another content id (`abp123` ->
     `118abp00123`), that id is only suggested, and you check that it is your video.
2. **ClearJAV** is asked what it already has of this video (it also finds its other ids). If there
   is something, it shows it and asks whether to upload yours anyway (e.g. better quality).
   Without anyone to ask (batch, scripts), the video is skipped unless `--allow-duplicates`.
   A torrent with exactly the same title is never uploaded: the site refuses a second one.
   A dry run says the same as a real upload, but asks nothing.
3. **MediaInfo** text, without your folders. Damaged, cut-off and empty files are refused (the reason
   is shown).
   The video is renamed (not in a dry run, which only says what it would rename).
4. **Contact sheet**: one per file, a shot every 2 minutes (every 4 minutes for videos over 4 hours), like the site's own uploads.
   ClearJAV takes sheets up to 4 MB: a larger one (a very long, detailed video) is made again with the
   shots further apart (10, 15, 20, 30 minutes), at the same quality; only if that is still too big is
   the picture quality lowered, and if nothing fits the video is refused.
5. **Torrent**: private, with ClearJAV's piece size and your announce URL if saved.
6. **Upload**: if the answer gets lost (also a Cloudflare timeout), the upload is sent again; the
   site recognises it and doesn't make a second torrent.
7. **The torrent to seed**: the site's own `.torrent` (with your passkey) is saved. Seed that
   one, not the one made in step 5. It is then put in the watch folder or added to qBittorrent,
   as set. If it can't be fetched or saved, the upload still went through: a note gives the
   torrent's page to download it from.

Everything is saved in `clearjav-uploads` next to the video, or in the folder of `--out`.

| Option | What it does |
|---|---|
| `-n`, `--dry-run` | Make everything but don't upload |
| `-r`, `--recursive` | Also look in subfolders |
| `-o`, `--out FOLDER` | Where to save the files |
| `--rename content_id\|dvd_id\|title` | What to rename the video to (default: content id) |
| `--anonymous` | Upload anonymously |
| `--personal [--tag TAG]` | A personal release (you made it yourself); the tag is optional |
| `--mod-queue` | Trusted members: send it to moderation anyway (everyone else's uploads always go there) |
| `--allow-duplicates` | Upload even when the video already has torrents |
| `--source DVD\|VHS\|Blu-Ray\|"3D Blu-Ray"\|"UHD Blu-Ray"` | Remuxes: the disc they come from |
| `--no-qbittorrent` | Only save the torrent to seed this time |

Staff and internal groups only (the tool refuses them for other accounts, as the site would ignore
them):

| Option | What it does |
|---|---|
| `--internal` | An internal release: tagged `-ClearJAV` |
| `--freeleech 25\|50\|75\|100 [--freeleech-days N]` | Freeleech, for N days or without end |
| `--double-upload [--double-upload-days N]` | Double upload, for N days or without end |
| `--sticky` | Pinned to the top of the torrent list |
| `--refundable` | Refundable download |
| `--featured` | In the featured slider |

Other commands: `gui` (the page; `--port`, `--no-browser`), `login`, `announce`, `logout` (removes
the key, announce URL and qBittorrent password), `qbittorrent`, `doctor`, `config`.

## Settings

The page's Settings tab, or `run.bat config`, which shows the settings file:
- Windows: `%APPDATA%\ClearJAV-AIO\config.ini`
- Linux: `~/.config/clearjav-aio/config.ini`

Defaults for renaming, anonymous uploads, personal release and tag, the output folder, seeding,
program locations and qBittorrent go there, and the page's look: `theme = system` (as the computer is set),
`dark` or `light`, and `covers = yes` or `no`.

- **Log**:
  - Windows: `%LOCALAPPDATA%\ClearJAV-AIO\Logs\clearjav-aio.log`
  - Linux: `~/.local/state/clearjav-aio/`

  API keys, passkeys and passwords are removed from it.
- **Exit codes** (terminal): `0` all uploaded, `1` some were not, `2` setup problem, `130` stopped.

## Development

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m unittest discover -s tests -t .
```

- **Tests**: they use a stand-in for ClearJAV's API and small clips made with FFmpeg. Tests that
  need FFmpeg and MediaInfo are skipped without them.
- **Font**: `clearjav_aio/fonts/` holds DejaVu Sans (mtn's font), with its licence.
- **The page**: `clearjav_aio/web.py` (server on 127.0.0.1; the address the tool opens carries a
  random key that becomes a cookie, so other web pages can't use it) and `clearjav_aio/web/`.
- **Windows download**:
  `powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1`
  makes `dist\ClearJAV-Uploader\` and its zip with PyInstaller. FFmpeg and MediaInfo are never put
  in it (the build stops if they are). It also stops when a file in the build names the computer it
  was made on (user name, folders, git account).
- **Icon**: `packaging\windows\make_icon.py` draws the program's icon, `cj.ico` (ClearJAV's seal), with
  Python alone; run it again after changing the seal.
