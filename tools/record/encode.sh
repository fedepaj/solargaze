#!/bin/bash
# encode.sh <frames dir> <docs dir> <gif dir>: the clips recorded by record.mjs, as the WebM and
# MP4 the app plays, their posters, the blurred backdrop of the connect screen, and the GIFs
# for the README (8 fps; 640 px for the hero, 440 for the guide).
set -euo pipefail
M=$1; D=$2; G=$3; mkdir -p "$G"
vid() { # frames fps w h name
  local f=$1 fps=$2 w=$3 h=$4 name=$5
  ffmpeg -v error -y -framerate $fps -i "$f/f%03d.png" -vf "scale=$w:$h:flags=lanczos" -c:v libvpx-vp9 -b:v 0 -crf 40 -row-mt 1 -pix_fmt yuv420p -an "$D/$name.webm"
  ffmpeg -v error -y -framerate $fps -i "$f/f%03d.png" -vf "scale=$w:$h:flags=lanczos" -c:v libx264 -crf 27 -preset slow -pix_fmt yuv420p -movflags +faststart -an "$D/$name.mp4"
}
gif() { # frames infps outfps w name
  local f=$1 infps=$2 outfps=$3 w=$4 name=$5
  ffmpeg -v error -y -framerate $infps -i "$f/f%03d.png" -vf "fps=$outfps,scale=$w:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" -loop 0 "$G/$name.gif"
}
poster() { # png w h out
  python3 -c "from PIL import Image; im=Image.open('$1').convert('RGB').resize(($2,$3), Image.LANCZOS); im.save('$4', 'WEBP', quality=78, method=6)"
}
vid "$M/hero" 8 640 370 colosseum-day
poster "$M/hero/f010.png" 640 371 "$D/colosseum-poster.webp"
gif "$M/hero" 8 8 640 colosseum-day
python3 -c "from PIL import Image, ImageFilter; im=Image.open('$M/blur/f000.png').convert('RGB').filter(ImageFilter.GaussianBlur(18)); im.save('$D/colosseum-blur.webp', 'WEBP', quality=60, method=6)"
for c in time date pin analyze heat; do
  [ -d "$M/$c" ] || continue
  vid "$M/$c" 12 720 432 guide-$c
  poster "$M/$c/f000.png" 720 432 "$D/guide-$c-poster.webp"
  gif "$M/$c" 12 8 440 guide-$c
done
ls -la "$D"/colosseum-* "$D"/guide-* "$G"
