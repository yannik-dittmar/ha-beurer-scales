# Minimum Home Assistant version is 2026.10

The integration relies on noticing every wake of the scale. HA 2026.10 added a callback that fires for every advertisement. Older versions only offer callbacks that fire when the advertisement *changes*, so two quick wakes with the same pending-slot list could go unnoticed. Do not lower the minimum version without replacing that mechanism. (Config subentries need 2025.3, and one device per subentry needs 2026.8; both are covered.)
