###ffcutter
Command line + gui utility to cut videos using ffmpeg.

![Screenshot](http://i.imgur.com/IwVuoMG.png)

###Installation
#####Debian/Ubuntu
```
> apt-get install ffmpeg python3-pyqt5 python3-docopt python3-colorama mpv
```
FFmpeg may not be available in default repositories. Refer to the [ffmpeg download page](https://ffmpeg.org/download.html#build-linux).
#####Archlinux
```
> pacman -S ffmpeg python-pyqt5 python-docopt python-colorama mpv
```
#####Windows
Someday...

###Usage
```
> ffcutter.py <video-file> [-s <save-file> --mpv=mpv-option...]
> ffcutter.py -h | --help
```
