# Interactive mockup assets

The main preview is a local HTML/React composition, using the actual `src/App.tsx`
with the browser adapter and isolated sample client. All pixels inside the device
display are rendered by the page. The hardware picture is an unchanged backdrop;
its old screen content is completely covered by the HTML display.

- `public/assets/legion-go-2-frame.png`: copy of `output/mockup/legion-go-2-spotify-qam-v2.png`, the previously generated imagegen mockup. Hardware is decorative. A/B/X, D-pad and QAM have HTML button overlays.
- Game cover art, fetched from Steam's asset CDN for the fictional library background:
  - https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/1145360/library_600x900_2x.jpg
  - https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/1245620/library_600x900_2x.jpg
  - https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/367520/library_600x900_2x.jpg
- QAM proportions and ordering are based on the screenshots inspected for v2:
  - https://steamdeckhq.com/wp-content/uploads/2023/05/DeckyInstalled.jpg
  - https://raw.githubusercontent.com/NGnius/PowerTools/main/assets/ui.png

The inner Steam canvas is exactly 1280 × 800 (16:10), uniformly scaled with a
ResizeObserver. Global status/footer are each 60 units high. The QAM is 522 units
wide, comprising a 72-unit rail on the left and 450-unit content on the right.
The browser adapter is shown at 125% inside that content. This is a controlled
mockup, not a measurement of the user's current Steam UI scaling or a live
Gamescope session.

Images, fictional data, simulated Steam frame, and browser input handling remain
inside `preview/`; they are excluded from the Decky plugin ZIP. Steam images belong
to their respective owners and are not covered by this project's source license.

Routes: `/` = whole handheld, `/?view=screen` = enlarged interactive screen,
`/?panel` = the original compact panel preview.
