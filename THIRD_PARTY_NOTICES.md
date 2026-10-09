# Third-party notices

## ScanSciPDF

The bundled public school gateway parameters in `src/schools.json` were derived
from the ScanSciPDF 1.9.0 school catalog, licensed under Apache License 2.0.
They are stored as project data and do not create a Python package or runtime
dependency on ScanSciPDF.

The fallback route for Elsevier / ScienceDirect PDFs through an institution
gateway (`src/providers/elsevier_institution_browser.py`) was designed with
reference to ScanSciPDF's institutional browser download approach. It is an
independent implementation.

Project: <https://github.com/Rimagination/scansci-pdf>

The applicable license text is included at
`third_party_licenses/Apache-2.0-ScanSciPDF.txt`.

## Azure ttk theme

The graphical interface includes Azure ttk theme by rdbende, revision
`997dbbef09563c3a0b2541ae3de5cd774f1640fb`, distributed under the MIT
License. The vendored runtime files are stored under `src/themes/azure/`.

The applicable license text is included at
`third_party_licenses/MIT-Azure-ttk-theme.txt`.
