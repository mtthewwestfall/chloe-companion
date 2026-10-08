# Archived Stripe Payment Links

These payment links were **disconnected from the tier system on 2026-10-08**
when the Companion app moved to a single $9.99/month tier. They were **NOT
deleted from Stripe** — they are still live in the Stripe dashboard and can
be repurposed for other things later.

## Companion Plus — $19.99/month (archived)

- Checkout URL: https://buy.stripe.com/bJecMYdHd38H81xfUldjO0d
- Payment link ID: `plink_1UKjPv6qicU1CK4UpaV1VAHb`
- Price ID: `price_1UKiwL6qicU1CK4UBFqunzeH`
- Was mapped to tier: `plus` (2,000 msgs + 60 voice notes)

## Companion VIP — $29.99/month (archived)

- Checkout URL: https://buy.stripe.com/4gM4gs8mT10zchN9vXdjO0e
- Payment link ID: `plink_1UKqaT6qicU1CK4UoO0GbptK`
- Price ID: `price_1UKqZl6qicU1CK4U5QNulgOY`
- Was mapped to tier: `vip` (3,000 msgs + 150 voice notes)

## Notes

- The webhook (`POST /api/v1/stripe/webhook`) no longer maps these price/link
  IDs to any tier. If one is reused, add its price/link ID back to
  `STRIPE_PRICE_TIERS` / `STRIPE_LINK_TIERS` in
  `chloe-companion-server/app/config.py`.
- The JVZoo totals 19.99/29.99 were also disconnected from tier fulfillment
  (see `JVZOO_TIER_TOTALS` in config.py).
- Trial link ($0.99, 100 messages) is unchanged and still live:
  https://buy.stripe.com/3cI14ggTpfVtbdJ4bDdjO0f
