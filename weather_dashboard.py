#!/usr/bin/env python3
"""
Weather Dashboard (OpenWeatherMap)

Features:
  - Current conditions + 5-day forecast for any city
  - Metric / imperial units
  - Response caching (default 10 minutes) to avoid repeated API calls
  - Favorite cities saved to disk
  - Clean error handling (bad key, unknown city, rate limit, no internet)
  - Two interfaces: command-line menu (default) and tkinter GUI (--gui)

Setup:
  1. Get a free API key at https://openweathermap.org/api
     (new keys can take up to ~2 hours to activate)
  2. pip install requests
  3. Set your key, either as an environment variable:
         export OPENWEATHER_API_KEY=your_key_here          (Linux/macOS)
         setx OPENWEATHER_API_KEY your_key_here            (Windows)
     or in a file named .env next to this script:
         OPENWEATHER_API_KEY=your_key_here

Run:
  python weather_dashboard.py             # CLI menu
  python weather_dashboard.py London      # quick lookup from the command line
  python weather_dashboard.py --gui       # tkinter window
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "weather_cache.json"
FAVORITES_FILE = BASE_DIR / "favorite_cities.json"
ENV_FILE = BASE_DIR / ".env"

API_BASE = "https://api.openweathermap.org/data/2.5"
CACHE_TTL_SECONDS = 10 * 60
REQUEST_TIMEOUT = 10

UNIT_SYMBOLS = {
    "metric": {"temp": "°C", "speed": "m/s"},
    "imperial": {"temp": "°F", "speed": "mph"},
}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def load_env_file(path: Path = ENV_FILE) -> None:
    """Minimal .env loader (KEY=value lines) so we don't need python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_api_key() -> str:
    load_env_file()
    key = os.environ.get("OPENWEATHER_API_KEY", "").strip()
    if not key:
        raise WeatherError(
            "No API key found. Set the OPENWEATHER_API_KEY environment variable "
            "or add it to a .env file (see the top of this script)."
        )
    return key


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class WeatherError(Exception):
    """Any problem fetching or parsing weather data, with a user-friendly message."""


# --------------------------------------------------------------------------
# Simple JSON cache
# --------------------------------------------------------------------------
class Cache:
    def __init__(self, path: Path = CACHE_FILE, ttl: int = CACHE_TTL_SECONDS):
        self.path = path
        self.ttl = ttl
        self._data = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data), encoding="utf-8")
        except OSError:
            pass  # cache is optional; never crash because of it

    def get(self, key: str):
        entry = self._data.get(key)
        if entry and time.time() - entry["time"] < self.ttl:
            return entry["payload"]
        return None

    def set(self, key: str, payload: dict) -> None:
        self._data[key] = {"time": time.time(), "payload": payload}
        self._save()


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------
class WeatherClient:
    def __init__(self, api_key: str, cache: Cache | None = None, session=None):
        self.api_key = api_key
        self.cache = cache or Cache()
        self.session = session or requests.Session()

    def _fetch(self, endpoint: str, city: str, units: str) -> dict:
        cache_key = f"{endpoint}|{city.lower()}|{units}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        params = {"q": city, "appid": self.api_key, "units": units}
        try:
            response = self.session.get(
                f"{API_BASE}/{endpoint}", params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.exceptions.ConnectionError:
            raise WeatherError("Could not connect. Check your internet connection.")
        except requests.exceptions.Timeout:
            raise WeatherError("The weather service took too long to respond.")
        except requests.exceptions.RequestException as exc:
            raise WeatherError(f"Network error: {exc}")

        if response.status_code == 401:
            raise WeatherError("Invalid API key (new keys can take a while to activate).")
        if response.status_code == 404:
            raise WeatherError(f"City '{city}' was not found. Try 'City,CountryCode' e.g. 'Paris,FR'.")
        if response.status_code == 429:
            raise WeatherError("API rate limit reached. Please wait a minute and try again.")
        if not response.ok:
            raise WeatherError(f"Weather service error (HTTP {response.status_code}).")

        try:
            payload = response.json()
        except ValueError:
            raise WeatherError("Received an invalid response from the weather service.")

        self.cache.set(cache_key, payload)
        return payload

    def current(self, city: str, units: str = "metric") -> dict:
        return parse_current(self._fetch("weather", city, units))

    def forecast(self, city: str, units: str = "metric") -> list[dict]:
        return parse_forecast(self._fetch("forecast", city, units))


# --------------------------------------------------------------------------
# Parsing (pure functions, easy to unit test)
# --------------------------------------------------------------------------
def local_time(ts: int, tz_offset: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(seconds=tz_offset)


def parse_current(data: dict) -> dict:
    try:
        tz = data.get("timezone", 0)
        return {
            "city": data["name"],
            "country": data.get("sys", {}).get("country", ""),
            "temp": data["main"]["temp"],
            "feels_like": data["main"]["feels_like"],
            "humidity": data["main"]["humidity"],
            "pressure": data["main"]["pressure"],
            "wind_speed": data.get("wind", {}).get("speed", 0),
            "description": data["weather"][0]["description"].capitalize(),
            "sunrise": local_time(data["sys"]["sunrise"], tz).strftime("%H:%M"),
            "sunset": local_time(data["sys"]["sunset"], tz).strftime("%H:%M"),
        }
    except (KeyError, IndexError, TypeError):
        raise WeatherError("Unexpected data format received from the weather service.")


def parse_forecast(data: dict) -> list[dict]:
    """Collapse the 3-hourly forecast into one summary per local day."""
    try:
        tz = data["city"].get("timezone", 0)
        days = defaultdict(list)
        for item in data["list"]:
            day = local_time(item["dt"], tz).date()
            days[day].append(item)

        summary = []
        for day in sorted(days):
            items = days[day]
            temps = [i["main"]["temp"] for i in items]
            descriptions = Counter(i["weather"][0]["description"] for i in items)
            summary.append(
                {
                    "date": day.strftime("%a %d %b"),
                    "min": min(temps),
                    "max": max(temps),
                    "description": descriptions.most_common(1)[0][0].capitalize(),
                    "rain_chance": max(i.get("pop", 0) for i in items),
                }
            )
        return summary[:5]
    except (KeyError, IndexError, TypeError):
        raise WeatherError("Unexpected data format received from the weather service.")


# --------------------------------------------------------------------------
# Favorites
# --------------------------------------------------------------------------
class Favorites:
    def __init__(self, path: Path = FAVORITES_FILE):
        self.path = path
        self.cities: list[str] = self._load()

    def _load(self) -> list[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [c for c in data if isinstance(c, str)]
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.cities, indent=2), encoding="utf-8")

    def add(self, city: str) -> bool:
        city = city.strip()
        if not city or city.lower() in (c.lower() for c in self.cities):
            return False
        self.cities.append(city)
        self._save()
        return True

    def remove(self, city: str) -> bool:
        for existing in self.cities:
            if existing.lower() == city.strip().lower():
                self.cities.remove(existing)
                self._save()
                return True
        return False


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
def format_current(w: dict, units: str) -> str:
    u = UNIT_SYMBOLS[units]
    place = f"{w['city']}, {w['country']}" if w["country"] else w["city"]
    return (
        f"\n=== {place} ===\n"
        f"{w['description']}\n"
        f"Temperature : {w['temp']:.1f}{u['temp']} (feels like {w['feels_like']:.1f}{u['temp']})\n"
        f"Humidity    : {w['humidity']}%\n"
        f"Pressure    : {w['pressure']} hPa\n"
        f"Wind        : {w['wind_speed']:.1f} {u['speed']}\n"
        f"Sunrise     : {w['sunrise']}    Sunset: {w['sunset']}"
    )


def format_forecast(days: list[dict], units: str) -> str:
    u = UNIT_SYMBOLS[units]["temp"]
    lines = ["\n5-day forecast:", f"{'Day':<12}{'Low':>8}{'High':>8}{'Rain':>7}  Conditions", "-" * 56]
    for d in days:
        lines.append(
            f"{d['date']:<12}{d['min']:>7.0f}{u[-1]}{d['max']:>7.0f}{u[-1]}"
            f"{d['rain_chance']:>6.0%}  {d['description']}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def show_city(client: WeatherClient, city: str, units: str) -> bool:
    try:
        print(format_current(client.current(city, units), units))
        print(format_forecast(client.forecast(city, units), units))
        return True
    except WeatherError as exc:
        print(f"\nError: {exc}")
        return False


CLI_MENU = """
===== WEATHER DASHBOARD  (units: {units}) =====
1) Look up a city
2) Show a favorite city
3) Add city to favorites
4) Remove city from favorites
5) Switch units (metric/imperial)
0) Quit
"""


def run_cli(client: WeatherClient, favorites: Favorites, units: str, first_city: str | None) -> None:
    last_city = None
    if first_city:
        if show_city(client, first_city, units):
            last_city = first_city

    while True:
        print(CLI_MENU.format(units=units))
        choice = input("Choose an option: ").strip()

        if choice == "0":
            print("Goodbye!")
            return

        elif choice == "1":
            city = input("City name (e.g. Chennai or Paris,FR): ").strip()
            if city and show_city(client, city, units):
                last_city = city

        elif choice == "2":
            if not favorites.cities:
                print("No favorites yet.")
                continue
            for i, c in enumerate(favorites.cities, 1):
                print(f"  {i}) {c}")
            pick = input("Number: ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(favorites.cities):
                city = favorites.cities[int(pick) - 1]
                if show_city(client, city, units):
                    last_city = city
            else:
                print("Invalid choice.")

        elif choice == "3":
            default = f" [{last_city}]" if last_city else ""
            city = input(f"City to add{default}: ").strip() or (last_city or "")
            print("Added." if favorites.add(city) else "Not added (empty or already a favorite).")

        elif choice == "4":
            city = input("City to remove: ")
            print("Removed." if favorites.remove(city) else "That city is not in your favorites.")

        elif choice == "5":
            units = "imperial" if units == "metric" else "metric"
            print(f"Units are now {units}.")

        else:
            print("Invalid option.")


# --------------------------------------------------------------------------
# GUI (tkinter)
# --------------------------------------------------------------------------
def run_gui(client: WeatherClient, favorites: Favorites, units: str) -> None:
    import threading
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Weather Dashboard")
    root.geometry("560x520")
    root.minsize(520, 480)

    units_var = tk.StringVar(value=units)
    city_var = tk.StringVar()
    status_var = tk.StringVar(value="Enter a city and press Search.")

    # --- top bar ---
    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    entry = ttk.Combobox(top, textvariable=city_var, values=favorites.cities, width=28)
    entry.grid(row=0, column=0, padx=(0, 6))
    search_btn = ttk.Button(top, text="Search")
    search_btn.grid(row=0, column=1, padx=3)
    fav_btn = ttk.Button(top, text="★ Save")
    fav_btn.grid(row=0, column=2, padx=3)
    unfav_btn = ttk.Button(top, text="Remove")
    unfav_btn.grid(row=0, column=3, padx=3)

    unit_frame = ttk.Frame(root, padding=(10, 0))
    unit_frame.pack(fill="x")
    ttk.Radiobutton(unit_frame, text="Metric (°C)", variable=units_var, value="metric").pack(side="left")
    ttk.Radiobutton(unit_frame, text="Imperial (°F)", variable=units_var, value="imperial").pack(side="left", padx=10)

    # --- current conditions ---
    current_box = ttk.LabelFrame(root, text="Current conditions", padding=10)
    current_box.pack(fill="x", padx=10, pady=10)
    big_temp = ttk.Label(current_box, text="--", font=("Helvetica", 32, "bold"))
    big_temp.pack(anchor="w")
    summary_lbl = ttk.Label(current_box, text="", font=("Helvetica", 12))
    summary_lbl.pack(anchor="w")
    details_lbl = ttk.Label(current_box, text="", justify="left")
    details_lbl.pack(anchor="w", pady=(6, 0))

    # --- forecast table ---
    fc_box = ttk.LabelFrame(root, text="5-day forecast", padding=6)
    fc_box.pack(fill="both", expand=True, padx=10, pady=(0, 6))
    cols = ("day", "low", "high", "rain", "cond")
    tree = ttk.Treeview(fc_box, columns=cols, show="headings", height=5)
    for col, text, width in [
        ("day", "Day", 90), ("low", "Low", 60), ("high", "High", 60),
        ("rain", "Rain", 60), ("cond", "Conditions", 200),
    ]:
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="center" if col != "cond" else "w")
    tree.pack(fill="both", expand=True)

    ttk.Label(root, textvariable=status_var, relief="sunken", anchor="w", padding=4).pack(fill="x", side="bottom")

    # --- behaviour ---
    def set_busy(busy: bool) -> None:
        search_btn.config(state="disabled" if busy else "normal")

    def show_result(current: dict, forecast: list[dict], u: str) -> None:
        sym = UNIT_SYMBOLS[u]
        place = f"{current['city']}, {current['country']}" if current["country"] else current["city"]
        big_temp.config(text=f"{current['temp']:.0f}{sym['temp']}")
        summary_lbl.config(text=f"{place} - {current['description']}")
        details_lbl.config(
            text=(
                f"Feels like {current['feels_like']:.0f}{sym['temp']}   |   "
                f"Humidity {current['humidity']}%   |   "
                f"Wind {current['wind_speed']:.1f} {sym['speed']}\n"
                f"Pressure {current['pressure']} hPa   |   "
                f"Sunrise {current['sunrise']}   |   Sunset {current['sunset']}"
            )
        )
        tree.delete(*tree.get_children())
        for d in forecast:
            tree.insert("", "end", values=(
                d["date"], f"{d['min']:.0f}{sym['temp'][-1]}", f"{d['max']:.0f}{sym['temp'][-1]}",
                f"{d['rain_chance']:.0%}", d["description"],
            ))
        status_var.set(f"Updated {datetime.now():%H:%M:%S}")

    def worker(city: str, u: str) -> None:
        try:
            current = client.current(city, u)
            forecast = client.forecast(city, u)
            root.after(0, lambda: (show_result(current, forecast, u), set_busy(False)))
        except WeatherError as exc:
            root.after(0, lambda: (status_var.set("Error"), messagebox.showerror("Weather error", str(exc)), set_busy(False)))

    def search(_event=None) -> None:
        city = city_var.get().strip()
        if not city:
            status_var.set("Please enter a city name.")
            return
        set_busy(True)
        status_var.set(f"Loading weather for {city}...")
        threading.Thread(target=worker, args=(city, units_var.get()), daemon=True).start()

    def refresh_favorites() -> None:
        entry["values"] = favorites.cities

    def save_fav() -> None:
        city = city_var.get().strip()
        status_var.set(f"Saved {city} to favorites." if favorites.add(city) else "Not saved (empty or already a favorite).")
        refresh_favorites()

    def remove_fav() -> None:
        city = city_var.get().strip()
        status_var.set(f"Removed {city}." if favorites.remove(city) else "That city is not in your favorites.")
        refresh_favorites()

    search_btn.config(command=search)
    fav_btn.config(command=save_fav)
    unfav_btn.config(command=remove_fav)
    entry.bind("<Return>", search)
    entry.bind("<<ComboboxSelected>>", search)

    root.mainloop()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Weather dashboard using OpenWeatherMap.")
    parser.add_argument("city", nargs="*", help="City to look up immediately (optional)")
    parser.add_argument("--gui", action="store_true", help="Open the tkinter window")
    parser.add_argument("--units", choices=["metric", "imperial"], default="metric")
    args = parser.parse_args()

    try:
        client = WeatherClient(get_api_key())
    except WeatherError as exc:
        print(f"Error: {exc}")
        return 1

    favorites = Favorites()

    try:
        if args.gui:
            run_gui(client, favorites, args.units)
        else:
            run_cli(client, favorites, args.units, " ".join(args.city) or None)
    except (KeyboardInterrupt, EOFError):
        print("\nGoodbye!")
    return 0


if __name__ == "__main__":
    sys.exit(main())