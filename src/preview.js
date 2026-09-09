export function previewDocument(html) {
  // A fresh opaque-origin frame and a restrictive policy keep project HTML away
  // from the workspace API and stop network requests in this static preview.
  return '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; font-src \'none\'; form-action \'none\'; base-uri \'none\'"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body>' + html + '</body></html>';
}
