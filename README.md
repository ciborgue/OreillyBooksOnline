# O'Reilly Books Online Downloader

## Problem Statement

This tool draws inspiration from existing solutions like [SafariBooks](https://github.com/lorenzodifuccia/safaribooks) but takes a different approach. Its primary aim is to ensure that downloaded books comply with EPUB standards as validated by [EPUBCheck](https://www.w3.org/publishing/epubcheck). Additionally, it leverages APIv2 and Python's asyncio library to enhance download speed, adding a touch of excitement to the process.

## Design Considerations

- Simplify the script for ease of use.
- Exclude support for Windows.
- Avoid incorporating credential management.
- Omit error recovery mechanisms.
- Compatible only with Firefox on Mac OS.
- Offer an optional feature for font conversion to WOFF2 format (requires the `woff2_compress` tool).

## Usage

1. Log in to the O'Reilly site using Firefox; this action stores access cookies.
2. Execute `./OreillyBooksOnline.py --email your_email --book-id nnnn [--woff2]`.
   - The email parameter is solely for login validation. Upon successful authentication, it should be visible on the profile page.
   - The `--woff2` flag instructs the script to convert downloaded fonts to WOFF2 format (ensure you have the `woff2_compress` tool installed and accessible in the PATH). Download it [here](https://tinyl.io/AbuY).
