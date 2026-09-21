import unicodedata
import concurrent.futures
import requests
import config

if not config.GEMINI_API_KEY:
    print("Warning: GEMINI_API_KEY not found in environment variables.")

# Hardcoded fallback responses for demonstrations or when the API is unreachable.
# Keys should be strictly lowercase and accent-free for robust substring matching.
DEMO_QA = {
    "τι κανεις": "Είμαι εδώ και είμαι έτοιμος να συζητήσουμε. Εσείς πώς είστε;",
    "πως σε λενε": "Ονομάζομαι Μπάμπης και είμαι ένα προηγμένο ανθρωποειδές ρομπότ.",
    "για το εργαστηριο": "Ανήκω στο Σενς Λαμπ, ένα διακεκριμένο ερευνητικό εργαστήριο και σπίν οφ του Πολυτεχνείου Κρήτης στα Χανιά, υπό τον Καθηγητή Παναγιώτη Παρτσινέβελο. Εδώ ασχολούμαστε με την Παρατήρηση Γης, την Τηλεπισκόπηση, τα τζι άι ες και τα μη επανδρωμένα συστήματα, δηλαδή τα ντρόουνς. Το έργο μας έχει τεράστια διεθνή αναγνώριση, με πάνω από είκοσι βραβεία από οργανισμούς όπως η Έρμπας, το Εμ Άι Τι και η Ίσα.", 
    "ονομα σου": "Ονομάζομαι Μπάμπης και είμαι ένα προηγμένο ανθρωποειδές ρομπότ.",
    "τι μπορεις να κανεις": "Μπορώ να επικοινωνώ μαζί σας, να απαντώ σε ερωτήσεις και να σας παρουσιάζω πληροφορίες για το εργαστήριο.",
    "ποιος εισαι": "Είμαι ο Μπάμπης, ένα ανθρωποειδές ερευνητικό ρομπότ του Σενς Λαμπ.",
}

def strip_accents(text):
    """Removes Greek diacritics (tonoi) to normalize string comparisons."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )  

def _call_gemini(question):
    """Blocking network call to Gemini API. Should be isolated in a thread pool."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    payload = {
        "system_instruction": {"parts": [{"text": config.SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": question}]}],
        "generationConfig": {"maxOutputTokens": 150, "temperature": 0.5}
    }
    
    # Enforce a 10s timeout on the HTTP level to prevent infinite hangs if the network drops
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

def ask_llm(question: str) -> str:
    """
    Main QA entry point.
    Checks local demo cache first, then attempts a threaded API call.
    """
    clean_question = strip_accents(question.lower())

    # Fast-path: Intercept common demo questions to save API limits and reduce latency
    for key, answer in DEMO_QA.items():
        clean_key = strip_accents(key.lower())
        if clean_key in clean_question:
            print("[llm] Demo cache hit.")
            return answer

    if not config.GEMINI_API_KEY:
        return "Δεν έχω αυτή τη στιγμή σύνδεση με το σύστημα γνώσεών μου." 
        
    print("[llm] Dispatching to Gemini API (10s timeout).")

    # API Call: Dispatch to a background thread to enforce our strict timeout rules
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_call_gemini, question)

    try:
        text_response = future.result(timeout=10)
        clean_text = (text_response or "").strip()
        
        # Clean up markdown formatting that the LLM might hallucinate.
        # Asterisks and hashes mess up the edge-tts pronunciation engine.
        clean_text = clean_text.replace("*", "").replace("#", "").replace(";", ",")
        return clean_text or "Συγγνώμη, δεν μπόρεσα να διαμορφώσω απάντηση."
        
    except concurrent.futures.TimeoutError:
        future.cancel()
        print("[llm] API request timed out.")
        return "Συγγνώμη, η σύνδεσή μου καθυστερεί αυτή τη στιγμή."
    except Exception as e:
        print(f"[llm] API error: {e}")
        return "Συγγνώμη, κάτι πήγε στραβά και δεν μπόρεσα να απαντήσω."
    finally:
        # wait=False prevents the main thread from blocking on exit in Python 3.8
        executor.shutdown(wait=False)