# Own integration instead of extending ha-beurer-bf600

[chalimov/ha-beurer-bf600](https://github.com/chalimov/ha-beurer-bf600) already reads BF105-family scales through ESPHome proxies. We build our own integration (domain `beurer_scales`, MIT) anyway. Keeping each person's readings separate would mean rebuilding that integration's core: it has one sensor set for all scale users, and we need a config subentry per scale user. It also has no license, so we could not legally build on its code. We may still link to it and compare protocol notes.
