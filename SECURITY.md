# Security policy

## Reporting a vulnerability

Please open a GitHub security advisory for vulnerabilities involving archive extraction, path traversal, unsafe CAD parsing, formula injection, command execution, or evidence/price integrity. Do not attach private drawings or quotation data to a public issue.

## Data handling

- Treat all CAD files, price books, review packs, manifests, renders, and quotations as potentially confidential.
- Keep real project inputs and generated run directories outside the repository.
- Do not commit access tokens, user paths, client names, drawing identifiers, material prices, or screenshots derived from client data.
- Run untrusted archives only through the bounded ingest stage.
- A generated workbook is a review draft unless every required evidence and price gate is `PASS`.
