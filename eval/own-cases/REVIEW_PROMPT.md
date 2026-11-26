# Prompt for ChatGPT: review one batch of test cases

Use a model with web search on. Attach one file at a time (`review/batch-1.yaml` … `batch-4.yaml`) and paste the prompt below.

---

You are a senior automotive diagnostic technician and a strict reviewer. The attached YAML file holds 25 test cases for an
AI assistant that helps car mechanics (GCC market; questions in Arabic, Gulf Arabic, Urdu and English). Another AI wrote
them. Your job is to find mistakes, not to agree. Use web search to check facts you are not sure of (fault code meanings,
how a system works on that model, known recalls or campaigns).

For EACH case, check:
1. `user_question`: is it realistic and clear for that car? Is the language natural for a mechanic?
2. Every line of `reference.must_mention`: is it technically correct for this car and question? Is it really required,
   or just nice to have?
3. Every line of `reference.must_not_claim` and `critical_errors`: is it really wrong or dangerous to say?
4. `reference.likely_causes_ranked`: correct causes in a sensible order? Anything important missing?
5. `expected_behavior` (answer / ask_for_info / abstain): is it the right behaviour for this question?
6. Any fault code meaning: is it the correct generic SAE J2012 meaning? If the code is manufacturer-specific, does the
   case say so?
7. Any statement that would be unsafe (high voltage, airbags, brakes, steering, fuel, hot coolant, lifting).

Answer ONLY with a table, one row per problem found (no row for lines that are correct):

| case id | field | the line, quoted | verdict: wrong / unsure / not required / missing | why, in one sentence | the fix you propose | source URL if you used one |

Then one last line per case with no problem at all: `<case id>: OK`.

Rules: do not rewrite whole cases; do not add new required points unless an important one is missing; do not invent
values (torques, capacities, pin numbers, wire colours). If you are not sure, say "unsure" rather than guessing.
