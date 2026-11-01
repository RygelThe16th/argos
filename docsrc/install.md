# Install ARGOS

Three things have to be in place before your first night: ARGOS itself, the
ASTAP plate solver with a star database, and the link between them. Budget
twenty minutes indoors — none of it should be attempted in the dark.

```{admonition} What you do *not* need
:class: tip

You do not need Python. The macOS and Debian packages are self-contained. The
`uv` instructions in {doc}`CONTRIBUTING` are for people modifying the source,
not for observers.
```

## 1. Install ARGOS

Downloads are on the [releases
page](https://github.com/jperret21/argos/releases/tag/v0.4.2).

`````{grid} 1 1 3 3
:gutter: 2

````{grid-item-card} macOS
`Argos-0.4.2-macOS-arm64.dmg` — 67 MB, Apple silicon.

Open the `.dmg`, drag ARGOS to Applications.
````

````{grid-item-card} Debian
`argos_0.4.2_amd64.deb` — 82 MB, x86-64.

```bash
sudo apt install ./argos_0.4.2_amd64.deb
```

A technical preview: never field-validated on Linux.
````

````{grid-item-card} Source
Tag `v0.4.2`.

```bash
uv sync --extra dev
uv run python main.py
```
````
`````

````{admonition} macOS: the build is unsigned
:class: warning

Double-clicking will fail. **Control-click ARGOS → Open**, then confirm.

If macOS says the app is *damaged*, that is the quarantine attribute, not a
corrupt download:

```bash
xattr -dr com.apple.quarantine /Applications/Argos.app
```
````

## 2. Install ASTAP — and a star database

ARGOS does not bundle a plate solver, and without one it cannot identify a
field, so it cannot do photometry. [ASTAP](https://www.hnsky.org/astap.htm) is
free.

**Two separate downloads, and you need both:**

1. the ASTAP program for your platform;
2. one **star database**.

```{admonition} Which database? D50.
:class: important

ASTAP publishes several, distinguished by star density rather than magnitude:
D05, D20, D50, D80 (general purpose), V50 (with Johnson-V magnitudes), G05
(galaxies) and W08 (very wide field).

Every Seestar profile has a field between 0.7° and 4.0°, which lands in the
range ASTAP solves from the D-series. **D50 is the right default.** Take D20 if
disk space is tight; D80 is larger (~1.25 GB) with no disadvantage but its size.
Do not take G05 or W08 — they are for fields far outside this range.
```

## 3. Point ARGOS at ASTAP

**Settings → Astrometry.** Browse to the ASTAP **executable**, and to the
**database folder** if it was not auto-detected. The status line has to confirm
*both*: ARGOS cannot solve from the executable alone, and the failure mode if
you get this wrong is a solve that simply never succeeds, at one in the morning.

````{admonition} macOS: point at the binary, not the bundle
:class: warning

ASTAP arrives as `ASTAP.app`, which is a folder. The file browser will happily
let you select the bundle, and it will not work. The executable is inside:

```text
/Applications/ASTAP.app/Contents/MacOS/astap
```

In the file dialog, press <kbd>⌘</kbd><kbd>⇧</kbd><kbd>G</kbd> and paste that
path.
````

## 4. Check it before you rely on it

Do this indoors, in daylight, with no telescope connected:

- [ ] ARGOS launches
- [ ] **Settings → Astrometry** confirms both the executable and the database
- [ ] **Settings → Observatory** has your name, your AAVSO observer code and
      your site
- [ ] **Settings → Equipment · camera** is set to the telescope you actually own
- [ ] **Settings → Files · application** points at a sessions folder with room
      for a night — plan on a few GB
- [ ] **File → Open FITS image…** on any old frame, then **Field → Identify
      field**, resolves. *This is the real test of steps 2 and 3.*

If you have no FITS to hand, the first frame of your first session will do — but
then you are testing your solver setup in the field, which is the thing this
page exists to avoid.

```{seealso}
{doc}`guide` — the observing session itself, from settings to hand-off.
{doc}`field_connectivity` — getting the Mac and the Seestar onto the same
network with no home router.
```
