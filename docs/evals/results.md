| case_id | spec_query | entry_mode | marketplaces | offers_total | offers_unique | marketplaces_with_listings | wall_clock_s | error_stops | est_cost_usd | passed_bar |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reliability_run_1 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 45 | 34 | 2 | 64.60356225003488 | 0 | 1.3182965 | True |
| reliability_run_2 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 54 | 39 | 2 | 104.46587362501305 | 0 | 2.0188824499999996 | True |
| reliability_run_3 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 76 | 47 | 2 | 93.38597674993798 | 0 | 2.1272673500000003 | True |
| reliability_run_4 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 37 | 31 | 2 | 49.36068066593725 | 1 | 1.0248669000000001 | False |
| reliability_run_5 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 44 | 32 | 2 | 70.99110537499655 | 0 | 1.43177035 | True |
| reliability_run_1 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 38 | 30 | 2 | 87.20085666701198 | 0 | 1.8303427 | True |
| reliability_run_2 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 38 | 25 | 2 | 78.5428471249761 | 0 | 1.7932401500000001 | True |
| reliability_run_3 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 45 | 34 | 2 | 72.35467158292886 | 0 | 1.5072227 | True |
| reliability_run_4 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 69 | 36 | 2 | 83.64235616708174 | 0 | 1.72234135 | True |
| reliability_run_5 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 45 | 27 | 2 | 107.57765929098241 | 0 | 1.9665848000000001 | True |

> **Campaign note (2026-07-21):** reliability_run_1–5 appear twice above.
> Campaign #1 scored 4/5 — run 4 crashed on an LLM empty-content response
> (`_parse_action` TypeError, fixed same day + regression test). Campaign #2
> (later rows), post-fix: **5/5**, 38–69 offers/run, 72–108s, 0 errors,
> $8.82 total. Cite campaign #2; keep #1 as the honest history.
| tuned_kijiji | Herman Miller Aeron chair Toronto | template | ['kijiji'] | 34 | 15 | 1 | 34.0619495830033 | 0 | 0.6623017499999999 | False | 0.97 |
| tuned_fb | Herman Miller Aeron chair Toronto | template | ['fb_marketplace'] | 22 | 18 | 1 | 61.657378582982346 | 0 | 1.35769445 | False | 0.91 |
| holdout_ebay_aeron | Herman Miller Aeron chair Toronto | template | ['ebay'] | 0 | 0 | 0 | 41.6017610830022 | 0 | 0.59014755 | False |  |
| holdout_craigslist_aeron | Herman Miller Aeron chair Toronto | template | ['craigslist'] | 0 | 0 | 0 | 8.39117983309552 | 0 | 0.04592835 | False |  |
| holdout_ebay_headphones | Sony WH-1000XM5 headphones | template | ['ebay'] | 2 | 2 | 1 | 20.278829624992795 | 0 | 0.17475190000000002 | False | 0.50 |
| reliability_run_1 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 38 | 25 | 2 | 43.55509070795961 | 0 | 0.9001191500000001 | True | 1.00 |
| reliability_run_2 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 35 | 28 | 2 | 54.349932542070746 | 0 | 1.4130586 | True | 0.97 |
| reliability_run_3 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 54 | 33 | 2 | 55.261747875018045 | 0 | 1.4056752500000003 | True | 0.87 |
| reliability_run_4 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 42 | 31 | 2 | 128.21144758397713 | 0 | 2.1233591 | True | 0.93 |
| reliability_run_5 | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 40 | 28 | 2 | 86.32866562507115 | 0 | 1.92043555 | True | 0.88 |
| tuned_kijiji | Herman Miller Aeron chair Toronto | template | ['kijiji'] | 0 | 0 | 0 | 652.8233297090046 | 3 | 0.0 | False |  |
| tuned_fb | Herman Miller Aeron chair Toronto | template | ['fb_marketplace'] | 0 | 0 | 0 | 653.9131227079779 | 3 | 0.0 | False |  |
| tuned_kijiji | Herman Miller Aeron chair Toronto | template | ['kijiji'] | 11 | 7 | 1 | 26.897254833951592 | 0 | 0.45251454999999996 | False | 1.00 |
| tuned_fb | Herman Miller Aeron chair Toronto | template | ['fb_marketplace'] | 29 | 26 | 1 | 68.17465162498411 | 0 | 1.39824505 | False | 0.90 |
| holdout_ebay_aeron | Herman Miller Aeron chair Toronto | template | ['ebay'] | 0 | 0 | 0 | 76.15048458403908 | 0 | 1.0685831500000003 | False |  |
| holdout_craigslist_aeron | Herman Miller Aeron chair Toronto | template | ['craigslist'] | 0 | 0 | 0 | 5.760848083999008 | 0 | 0.03520310000000001 | False |  |
| holdout_ebay_headphones | Sony WH-1000XM5 headphones | template | ['ebay'] | 3 | 2 | 1 | 49.45580658293329 | 0 | 0.6336348500000001 | False | 0.67 |
| ablation_studeal_gpt_4o | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 33 | 27 | 2 | 77.9839004590176 | 0 | 1.5719542499999999 | True | 1.00 |
| ablation_studeal_gpt_4o_mini | Herman Miller Aeron chair Toronto | template | ['kijiji', 'fb_marketplace', 'craigslist'] | 36 | 24 | 2 | 148.97392733301967 | 0 | 0.544 | True | 0.97 |
| ablation_naive_gpt_4o | Herman Miller Aeron chair Toronto | naive_react | ['kijiji', 'fb_marketplace', 'craigslist'] | 32 | 10 | 1 | 209.95494258299004 | 0 | 2.6659075000000003 | False | 0.97 |
| ablation_naive_gpt_4o_mini | Herman Miller Aeron chair Toronto | naive_react | ['kijiji', 'fb_marketplace', 'craigslist'] | 36 | 13 | 1 | 163.9311892919941 | 0 | 0.1599783 | False | 0.89 |
| entry_kijiji_template | Herman Miller Aeron chair Toronto | template | ['kijiji'] | 3 | 3 | 1 | 20.11415899998974 | 0 | 0.2151184 | False | 1.00 |
| entry_kijiji_home | Herman Miller Aeron chair Toronto | home | ['kijiji'] | 2 | 2 | 1 | 18.990449582925066 | 0 | 0.1487263 | False | 0.50 |
| entry_ebay_template | Herman Miller Aeron chair Toronto | template | ['ebay'] | 0 | 0 | 0 | 15.286295416997746 | 0 | 0.0484402 | False |  |
| entry_ebay_home | Herman Miller Aeron chair Toronto | home | ['ebay'] | 4 | 4 | 1 | 49.97078504203819 | 0 | 0.42430475000000006 | False | 0.75 |
| holdout_ebay_aeron | Herman Miller Aeron chair Toronto | template | ['ebay'] | 14 | 10 | 1 | 74.1213817500975 | 0 | 0.8081151000000002 | False | 0.71 |
| holdout_craigslist_aeron | Herman Miller Aeron chair Toronto | template | ['craigslist'] | 0 | 0 | 0 | 8.93083329219371 | 0 | 0.03491470000000001 | False |  |
| holdout_ebay_headphones | Sony WH-1000XM5 headphones | template | ['ebay'] | 0 | 0 | 0 | 57.27011016593315 | 0 | 0.8825611 | False |  |
