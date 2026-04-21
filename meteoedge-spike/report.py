from config import SETTLEMENTS_CSV

def main():
    if not SETTLEMENTS_CSV.exists():
        print("No settlements yet.")
        return

if __name__ == "__main__":
    main()
