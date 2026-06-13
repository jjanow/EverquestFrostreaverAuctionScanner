# EverquestFrostreaverAuctionScanner

A cross-platform Python GUI utility for finding recent seller listings on the
Frostreaver server using the [TLP Auctions API](https://www.tlp-auctions.com/swagger/index.html).

## Features

- Enter wanted items one per line, or import/export a text watchlist.
- Resolve item names through the Frostreaver item catalog.
- Fetch recent seller listings in bulk from the API.
- Show seller, price, timestamp, and how long ago the item was listed.
- Copy a ready-to-paste in-game inquiry for the selected seller:
  `/tell Seller Hi, is your Item still available? What price are you asking?`

## Requirements

- Python 3.11 or newer.
- Tkinter, which is included with most Python installers.

On some Linux distributions, Tkinter is packaged separately:

```bash
sudo apt install python3-tk
```

No third-party Python packages are required.

## Run

```bash
python3 auction_scanner.py
```

On Windows, use:

```powershell
py auction_scanner.py
```

The app stores its saved watchlist and catalog cache in
`~/.eq_auction_scanner/`.

## Usage

1. Add item names to the watchlist, one per line.
2. Click `Search Watchlist`.
3. Select a seller row.
4. Click `Copy Inquiry To Clipboard`.
5. Paste the copied `/tell` into EverQuest.

Exact item names work best. Unique partial names also work; ambiguous partial
matches are reported in the status line and skipped until the item name is made
more specific.
