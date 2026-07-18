# Royalty-free music

Add between one and five (or more) `.mp3` tracks to this directory. The renderer discovers MP3 files automatically and randomly selects one for each Short, so no code changes are needed.

Only add music you own or music whose license explicitly permits use in monetized YouTube videos. Keep a copy of the license and the download/source URL outside the repository. Do not assume that “free to download” means royalty-free or safe for YouTube.

Recommended preparation:

- Use instrumental tracks without recognizable copyrighted samples.
- Prefer tracks at least 20 seconds long; shorter tracks are looped automatically.
- Use normal MP3 encoding. FFmpeg will convert the selected track to AAC.
- Give files simple names such as `calm-01.mp3`.

The workflow intentionally fails with a clear message when this directory contains no MP3 file. Music is mixed at low volume with a one-second fade-in and two-second fade-out.

