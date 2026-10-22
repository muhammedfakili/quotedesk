# Upwork portfolio entry: QuoteDesk

Same layout as the other projects: title, role, 5 skills, one paragraph (600-character limit), cover + 5 images.

**Title** (59 characters)
QuoteDesk: AI Agent Turns Emailed RFQs into Approved Quotes

**Role**
AI Engineer

**Skills**
AI Agent Development · CrewAI · Python · Data Extraction · FastAPI

**Description** (533 / 600 characters)
Customer emails a PDF, Excel, scan or phone photo asking for prices. CrewAI agents classify it, extract every line and match it to a 288-SKU catalogue served over MCP; code prices it (margins, floor, units, currency) and drafts the quote PDF; a person approves before anything is sent. Measured on 36 test emails / 169 lines: 100% extracted, 100% right product, 0 silent errors vs 7 for a one-prompt baseline; injection attempt flagged, 0 data leaks. About $0.004 per email on Gemini Flash-Lite. FastAPI, Streamlit, Docker, 62 tests.

**Images, in this order**
| File | Shows |
|---|---|
| `00-cover.png` | Cover: black background, robot, "EMAIL TO / QUOTE / AI AGENT" |
| `01-injection-flagged.png` | An email that tells the "AI" to give 60% off and send the quote elsewhere: flagged, prices and recipient unchanged |
| `02-lines-and-flags.png` | "Ball valves" with no size: flagged with the matcher's reason, the 5 catalogue candidates and why each was found |
| `03-original-email.png` | The phone photo of a list that the quote was built from |
| `04-quote-pdf.png` | The approved quote: reply email and the PDF on the fictional letterhead |
| `05-measured-results.png` | Baseline vs crew on 36 emails, and keyword vs embeddings vs hybrid search |

Every number comes from `eval/results/` (crew.json, single.json, retrieval.json).
Images are reproducible: `shots.py` (Playwright against the running app) and `chart.py` in this folder.
The data is synthetic and the business fictional; say so if a client asks.
