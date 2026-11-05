# Mieszko Promo Manager

A standalone, promotion-only app for Ubuntu: product catalogue, Excel import, images, promotion prices, one-product-per-A4 PDF.

## Start

```bash
cp .env.example .env
# Set a strong POSTGRES_PASSWORD in .env
docker compose up -d --build
```

App on `127.0.0.1:8085` (use SSH tunnel or authenticated reverse proxy).

## Import layout

Excel **Product List** sheet: A internal code, B barcode, C brand, D unit, E weight, F name. Import existing products before adding images and promotions.

## Security / production status

**Prototype only, not production-ready.** No authentication/RBAC or migrations yet. Do not expose this service publicly. Change the `.env` secret, set up backups and access controls. API currently supports synchronous imports and up to 200 PDF labels at a time. Performance at 50,000 records is not yet benchmarked.
