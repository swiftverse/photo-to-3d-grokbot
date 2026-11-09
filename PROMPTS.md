# Prompts used with Grok Bot

These are the messages that drive the whole build. Grok Bot has its own Linux computer, so it installs packages, runs the scripts, looks at the frames it renders and fixes problems without you touching a terminal. Paste them one at a time; each one waits for the previous result.

## 1. Find a photo you are allowed to use

```
Find me a CC0 or public domain photo of a single car on Wikimedia Commons, shot
at a three-quarter angle with clear depth: the car close to the camera, then grass
or road, then a background. No readable number plates, no recognisable faces.
Give me the file page URL, the author and the exact licence, then download a
1280px version to /workspace/photo-3d/car.jpg and show it to me.
```

## 2. Depth map

```
On your box, install CPU PyTorch and transformers, run Depth Anything V2 Small
(depth-anything/Depth-Anything-V2-Small-hf) on car.jpg, and save a colourised
depth map plus a side-by-side of photo and depth. Time the step and show me the
side-by-side.
```

## 3. Mesh and .glb

```
Turn the depth map into a textured 3D mesh: back-project a 512-wide vertex grid
through a pinhole camera with a 60 degree horizontal FOV, map inverse depth to a
1 to 9 depth range, use the photo as the texture, and delete triangles that
stretch across depth edges (depth ratio above 1.05). Add a second background
layer behind it: push the car back to the depth of the scene on either side of
it, fill the car's pixels by mirroring the background next to it (so grass stays
grass), and extend that layer 20% past the photo edges so orbiting never shows
the edge of the world. Export scene.glb and tell me the vertex and face counts.
```

## 4. Interactive viewer

```
Build one self-contained HTML file with three.js that embeds the .glb and lets me
drag to orbit, scroll to zoom and double-click to reset. Limit the orbit so I
cannot swing far enough to see the back of the scene. No CDN, everything inline.
```

## 5. Video and GIF

```
Render a seamless 5 second camera loop from that viewer in headless Chrome,
orbiting 6 degrees each way around the car itself: a 1280x720 MP4 at 30 fps
and a looping GIF under 10 MB so it plays smoothly on GitHub and posts to X.
Look at the frames at 0%, 25%, 50% and 75% and fix anything ugly: grey edges,
holes, rubber-sheet smears.
```

## 5b. The fixes this run actually needed

The first render had three problems. These are the follow-ups that fixed them:

```
The frames crop the bumper and the camera orbits a point in front of the car.
Pivot the orbit on the car (0.95 x median depth), frame 97% of the photo and
cut the push-in to 4%.
```

```
When the camera swings right there is a smeared olive band next to the hood.
The fill layer still has the car's depth there because the min-filter is
smaller than the car. Make the filter wide and short (80% of the image width,
9 rows tall), and replace Telea inpainting with a row-by-row mirror of the
background on each side of the car.
```

```
At the far end of the swing a grey strip shows at the frame edge. Extend the
fill layer past the photo border with mirrored texture, and drop the viewer's
drag limit from 14 to 10 degrees.
```

## 6. Package it

```
Put the final script, the viewer template, requirements.txt, a README with the
GIF at the top, an MIT licence and a .gitignore in a repo folder. Clone it fresh,
install from requirements.txt in a new venv and run the one command again to
prove it works. Do not push anywhere.
```

## One-shot version

```
Here is a photo of a car (or find a CC0 one). Make it 3D on your box:
Depth Anything V2 Small depth map, textured mesh with edge trimming and a
filled background layer, .glb export, a single-file three.js viewer I can
drag around, a 5 second orbit MP4 and a GIF under 10 MB. Check the frames
yourself, then give me the files and the timings for each step.
```
