ARGOS documentation
===================

ARGOS is desktop observing software that turns a smart telescope into a
time-series photometry instrument. It records raw FITS with a complete
scientific header, solves the field, measures a differential light curve while
the sequence runs, and leaves behind a session folder you can reduce
independently.

The Seestar S30 Pro is the reference profile; the S30 and S50 profiles remain
explicitly unvalidated for precision photometry.

.. admonition:: 0.4.2 is a field-validation release
   :class: warning

   It is meant for observers who keep their raw frames and reduce them
   themselves. There is no weather safety and no restart recovery, so operate
   the telescope attended — and do not treat the live curve as a publishable
   measurement. What it does and does not mean is stated in
   :doc:`differential_photometry`.

Start here
----------

.. grid:: 1 2 2 2
   :gutter: 3

   .. grid-item-card:: ⬇  Install ARGOS
      :link: install
      :link-type: doc

      Twenty minutes indoors: the package, the ASTAP solver and its star
      database, and the link between them.

   .. grid-item-card:: 🔭  Observing with ARGOS
      :link: guide
      :link-type: doc

      One complete session, from the settings you fill in indoors to the folder
      you hand to Siril at dawn.

   .. grid-item-card:: 📐  Method and equations
      :link: differential_photometry
      :link-type: doc

      What is actually computed — apertures, ensemble zero point, uncertainty
      budget — with every formula as implemented.

   .. grid-item-card:: 🧭  FITS headers
      :link: fits_headers
      :link-type: doc

      The 65 keywords ARGOS writes, the file naming scheme and the session
      layout Siril expects.

   .. grid-item-card:: 📖  Glossary
      :link: glossary
      :link-type: doc

      AUID, check star, ensemble, zero point, green plane, TG — every term this
      documentation uses, defined once.

   .. grid-item-card:: 🛠  Developer guide

      :doc:`Architecture <ARCHITECTURE>` — layer map, threading model, data
      flow. :doc:`Seestar protocol <seestar_protocol>` — the wire format.
      :doc:`API reference <api/index>` — every module, from its docstrings.

What it does
------------

The Seestar S30 Pro is a remarkable little telescope for its price, but its
native software writes FITS with minimal metadata and offers no scientific
workflow beyond "save an image". ARGOS fills that gap.

.. grid:: 1 1 2 2
   :gutter: 2

   .. grid-item-card:: Talks to the telescope

      An ASCOM Alpaca driver layer reaches the Seestar over Wi-Fi, and the same
      layer drives the Alpaca simulator — so the whole application is testable
      without hardware. Jogging falls back to the native JSON-RPC port.

   .. grid-item-card:: Knows where it is pointing

      ASTAP recovers a WCS from the green plane, so pixel positions can be
      matched against Gaia DR3, AAVSO VSX and VSP, SIMBAD and the NASA
      Exoplanet Archive, and drawn as overlays on the image.

   .. grid-item-card:: Measures while it observes

      Circular-aperture photometry with a sky annulus, a CCD-equation error
      budget, an ensemble zero point with robust rejection, and a run-level
      systematic floor measured from the curve itself.

   .. grid-item-card:: Leaves an auditable trail

      Per-frame HFD, FWHM, eccentricity, star count and sky level in the FITS
      headers and in a JSON session record — plus an offline Review workspace
      to read the night back.

.. admonition:: The two-pipeline rule
   :class: important

   **Display and data are separate pipelines.** The raw 16-bit CFA array that
   comes off the sensor is never modified: it is exactly what gets written to
   FITS. The screen stretch (linear / log / asinh), the demosaicing
   (super-pixel / bilinear / raw) and the histogram are all computed from a
   *copy*. You can tune the display all night without ever touching the science
   frame.

Where the boundary is
---------------------

ARGOS measures raw, uncalibrated sub-exposures. That is enough to see that a
variable is varying and that the night is usable — it is not a calibrated
measurement. Darks, flats, registration and final photometry belong downstream,
to `Siril <https://siril.org>`__ and the companion ``star_var_script``, working
on the raw frames ARGOS preserves. The handover is described in :doc:`guide`,
and the limits in :doc:`differential_photometry`.

.. toctree::
   :hidden:
   :caption: Observing with ARGOS
   :name: sec-guide

   install.md
   guide.md
   field_identification.md
   exoplanet_transits.md
   field_connectivity.md

.. toctree::
   :hidden:
   :caption: Method and data
   :name: sec-method

   differential_photometry.md
   fits_headers.md
   glossary.md
   references.md

.. toctree::
   :hidden:
   :caption: Developer guide
   :name: sec-developer

   ARCHITECTURE.md
   seestar_protocol.md
   simulator_testing.md
   CONTRIBUTING.md

.. toctree::
   :hidden:
   :caption: API reference
   :name: sec-api

   api/index
