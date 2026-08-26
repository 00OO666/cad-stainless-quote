# Third-party notices

This repository contains original project code under the MIT License. It does not vendor the packages or external applications listed below; installers and package managers obtain them from their respective publishers. Each dependency remains subject to its own license.

## Direct Python dependencies

| Dependency | License family | Purpose |
|---|---|---|
| ezdxf | MIT | DXF parsing, audit, and drawing frontend |
| openpyxl | MIT | Reading XLSX and validating generated workbooks |
| Pydantic | MIT | Data contracts and validation |
| XlsxWriter | BSD-2-Clause | Portable formula-driven XLSX export |
| xlrd | BSD | Legacy XLS reading |
| rarfile | ISC | RAR metadata and extraction integration |
| py7zr | LGPL-2.1-or-later | 7z archive handling |
| PyYAML | MIT | Configuration data |
| Matplotlib | PSF-based, BSD-compatible | Headless CAD evidence PNG rendering |
| RapidFuzz | MIT | Conservative text similarity |
| Shapely | BSD-3-Clause | Geometry operations |

Consult the installed distribution metadata and upstream repositories for complete license texts and transitive dependencies. XlsxWriter's upstream license page identifies BSD-2-Clause; Matplotlib documents its PSF-based BSD-compatible license.

## External applications

### DWG converters

DWG conversion is an optional adapter. This repository does not download, bundle, redistribute, or license a converter. Supported discovery targets include ODA File Converter, AutoCAD Core Console, and `dwg2dxf` implementations. Users are responsible for selecting a backend and complying with its license.

Open Design Alliance states that non-members may use ODA Viewer/File Converter for non-commercial applications only. Commercial users should obtain suitable rights or choose another licensed backend:

- https://www.opendesign.com/faq/question/what-are-oda-viewer-and-oda-file-converter
- https://www.opendesign.com/guestfiles/oda_file_converter

### 7-Zip

7-Zip is an optional external backend for advertised RAR and 7z extraction support and is not bundled. Its upstream license page describes the applicable LGPL/BSD and unRAR restrictions:

- https://www.7-zip.org/license.txt
