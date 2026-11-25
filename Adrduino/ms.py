"""Ovladani dvou DC motoru na Arduino + Motor Shield L293D z PC.

Instalace:  pip install pyserial
Spusteni:   python ovladani_motoru.py COM3        (Windows)
            python ovladani_motoru.py /dev/ttyACM0 (Linux)

Prikazy v konzoli:
  1 200     motor 1 vpred rychlosti 200 (-255..255, zaporne = vzad)
  2 -120    motor 2 vzad rychlosti 120
  s         zastavit oba motory
  q         konec (motory se zastavi)
"""
import sys
import time
import serial


def posli(ser, text):
    ser.write((text + "\n").encode())
    odpoved = ser.readline().decode(errors="ignore").strip()
    print("  <-", odpoved)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    with serial.Serial(sys.argv[1], 9600, timeout=2) as ser:
        time.sleep(2)  # Arduino se po otevreni portu restartuje
        ser.reset_input_buffer()
        print("Pripojeno. Napis prikaz (napr. '1 200', '2 -150', 's', 'q').")
        try:
            while True:
                radek = input("> ").strip().lower()
                if radek == "q":
                    break
                if radek == "s":
                    posli(ser, "STOP")
                    continue
                casti = radek.split()
                if len(casti) == 2 and casti[0] in ("1", "2"):
                    try:
                        rychlost = max(-255, min(255, int(casti[1])))
                    except ValueError:
                        print("Rychlost musi byt cele cislo.")
                        continue
                    posli(ser, f"M{casti[0]} {rychlost}")
                else:
                    print("Neznamy prikaz.")
        finally:
            posli(ser, "STOP")


if __name__ == "__main__":
    main()