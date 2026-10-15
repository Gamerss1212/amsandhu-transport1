"""voice_loop_local.py - the whole voice pipeline with the audio parts swapped for the keyboard.

  you say / type  ->  [speech-to-text]  ->  voice_router (fixed list, no AI)  ->  execute  ->  [text-to-speech]

Run:  python voice_loop_local.py --report report.json --verified
Type 'status', 'report', 'halt', 'pause A073'. Anything else is refused. 'quit' exits.

To make it speak for real, replace listen() and speak() (see README: Whisper/Deepgram in, Piper/ElevenLabs/Fish out,
or Pipecat/LiveKit to do both and add a phone number). Keep the router and execute() exactly as they are.
"""
import argparse, os
import policy, voice_router

def listen() -> str:            # swap for speech-to-text
    return input("you> ")

def speak(text: str) -> None:   # swap for text-to-speech
    print("JARVUS>", text)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="report.json")
    ap.add_argument("--state-dir", default=".")
    ap.add_argument("--verified", action="store_true", help="pretend the caller is verified")
    a = ap.parse_args()
    cfg = policy.Config(state_dir=a.state_dir)
    audit = policy.AuditLog(cfg.audit_file)
    speak("Jarvus online. Paper only.")
    while True:
        try: text = listen()
        except EOFError: break
        if text.strip().lower() in ("quit", "exit"): break
        speak(voice_router.execute(voice_router.route(text, a.verified), cfg, a.report, audit=audit))

if __name__ == "__main__":
    main()
