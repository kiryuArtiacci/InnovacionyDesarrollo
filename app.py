import tkinter as tk
from tkinter import messagebox, Frame, Label, Entry, Button, StringVar, OptionMenu
import csv
import os
import folium
import webbrowser
import pandas as pd
from folium.plugins import MarkerCluster
import numpy as np
import http.server
import socketserver
import threading
from urllib.parse import urlparse, parse_qs
import requests
import time
import queue

# --- CONFIGURACIÓN ---
CSV_FILE = "ubicaciones_aguilas.csv"
SERVER_PORT = 8080
ELEVATION_API_URL = "https://api.open-meteo.com/v1/elevation"
REVERSE_GEO_API_URL = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"
GBIF_API_URL = "https://api.gbif.org/v1/occurrence/search"
PREY_TAXA = ["Bradypus", "Choloepus", "Alouatta", "Cebus", "Sapajus"]


def calculate_comment_weight(comment):
    if not isinstance(comment, str):
        return 0.5
    comment = comment.lower()
    weight = 1.0
    high_confidence = {
        "nido": 3.0,
        "pichón": 2.5,
        "adulto en nido": 3.5,
        "llevando presa": 2.0,
        "construyendo": 2.5,
        "pareja": 2.0,
    }
    low_confidence = {
        "lejos": -0.5,
        "no estoy seguro": -1.0,
        "canto lejano": -0.8,
        "creo que": -0.5,
        "posiblemente": -0.4,
    }
    for k, v in high_confidence.items():
        if k in comment:
            weight *= v
    for k, v in low_confidence.items():
        if k in comment:
            weight += v
    return max(0.1, weight)


def check_forest_cover(lat, lon, radius_m=50):
    query = f"""[out:json];(
        node["landuse"="forest"](around:{radius_m},{lat},{lon});
        way["landuse"="forest"](around:{radius_m},{lat},{lon});
        relation["landuse"="forest"](around:{radius_m},{lat},{lon});
        node["natural"="wood"](around:{radius_m},{lat},{lon});
        way["natural"="wood"](around:{radius_m},{lat},{lon});
        relation["natural"="wood"](around:{radius_m},{lat},{lon});
    );out geom;"""
    try:
        r = requests.post(OVERPASS_API_URL, data=query, timeout=10)
        r.raise_for_status()
        return len(r.json()["elements"]) > 0
    except requests.RequestException as e:
        print(f"Error API Overpass: {e}")
        return False


# <<<--- FUNCIÓN CORREGIDA PARA LA API DE GBIF ---<<<
def check_prey_availability(lat, lon, radius_km=10):
    # Convertir radio en km a grados de latitud/longitud (aproximación)
    deg_radius_lat = radius_km / 111.0
    deg_radius_lon = radius_km / (111.0 * np.cos(np.deg2rad(lat)))

    # Coordenadas del polígono cuadrado
    min_lon, max_lon = lon - deg_radius_lon, lon + deg_radius_lon
    min_lat, max_lat = lat - deg_radius_lat, lat + deg_radius_lat

    # Crear la cadena de polígono WKT
    wkt_polygon = (
        f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, "
        f"{max_lon} {max_lat}, {min_lon} {max_lat}, {min_lon} {min_lat}))"
    )

    total_prey_count = 0
    for genus in PREY_TAXA:
        params = {"genus": genus, "geometry": wkt_polygon, "limit": 1}
        try:
            response = requests.get(GBIF_API_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("count", 0) > 0:
                total_prey_count += data["count"]
        except requests.RequestException as e:
            # Ahora este error no debería ocurrir, pero lo mantenemos por seguridad
            print(f"Error API GBIF para {genus}: {e}")
            continue
    return total_prey_count


def get_location_viability(lat, lon):
    score = 0
    try:
        r = requests.get(
            ELEVATION_API_URL, params={"latitude": lat, "longitude": lon}, timeout=5
        )
        r.raise_for_status()
        elev = r.json()["elevation"][0]
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or elev <= 0:
            return False, f"Inviable (Fuera rango/agua. Elev: {elev}m)", 0
    except requests.RequestException as e:
        print(f"Error API Elevación: {e}")
        return False, "Error API Elevación", 0
    try:
        headers = {"User-Agent": "HarpiaNestApp/1.0"}
        r = requests.get(
            REVERSE_GEO_API_URL,
            params={"lat": lat, "lon": lon, "format": "jsonv2"},
            headers=headers,
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        cat, p_type = data.get("category", ""), data.get("type", "")
        excl_cats, excl_types = (
            ["water", "waterway"],
            [
                "city",
                "town",
                "village",
                "hamlet",
                "residential",
                "commercial",
                "industrial",
            ],
        )
        if cat in excl_cats or p_type in excl_types:
            return False, f"Inviable (Poblado/agua: {p_type or cat})", 0
    except requests.RequestException as e:
        print(f"Error API Geo-Inversa: {e}")
        return False, "Error API Geo-Inversa", 0

    reasons = []
    if check_forest_cover(lat, lon):
        score += 50
        reasons.append("Boscoso")
    else:
        return False, "Inviable (Hábitat no boscoso)", 0
    prey_count = check_prey_availability(lat, lon)
    if prey_count > 0:
        prey_score = min(50, int(10 * np.log1p(prey_count)))
        score += prey_score
        reasons.append(f"Presas: {prey_count} (P: +{prey_score})")
    else:
        reasons.append("Sin presas")

    return True, ", ".join(reasons), score


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Nest-Guesser")
        self.root.geometry("450x500")
        if not self.setup_csv():
            self.root.destroy()
            return
        self.httpd = None
        self.start_server()
        self.generation_queue = queue.Queue()
        if self.httpd:
            self.create_widgets()
            self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
            self.process_generation_queue()

    def setup_csv(self):
        HEADER = [
            "id",
            "lat",
            "lon",
            "tipo",
            "comentario",
            "puntuacion",
            "razon_validacion",
        ]
        if os.path.exists(CSV_FILE):
            try:
                with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
                    if next(csv.reader(f)) == HEADER:
                        return True
                os.rename(CSV_FILE, CSV_FILE + ".bak")
                messagebox.showwarning(
                    "Formato Antiguo",
                    "CSV antiguo detectado. Se ha creado una copia de seguridad y se generará un archivo nuevo.",
                )
            except (StopIteration, IndexError):
                pass
            except Exception as e:
                messagebox.showerror("Error", f"No se pudo verificar CSV: {e}")
                return False
        try:
            with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(HEADER)
            return True
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo crear CSV: {e}")
            return False

    def _get_next_id(self):
        try:
            df = pd.read_csv(CSV_FILE)
            return 1 if df.empty else int(df["id"].max()) + 1
        except (FileNotFoundError, pd.errors.EmptyDataError):
            return 1

    def create_widgets(self):
        main = Frame(self.root, padx=10, pady=10)
        main.pack(fill=tk.BOTH, expand=True)
        reg_f = Frame(main, padx=10, pady=10, relief=tk.RIDGE, borderwidth=2)
        reg_f.pack(pady=5, padx=5, fill=tk.X)
        Label(reg_f, text="Registrar Ubicación", font=("Helvetica", 12, "bold")).grid(
            row=0, column=0, columnspan=2, pady=5
        )
        Label(reg_f, text="Latitud:").grid(row=1, column=0, sticky="w", pady=2)
        self.entry_lat = Entry(reg_f)
        self.entry_lat.grid(row=1, column=1, pady=2, sticky="ew")
        Label(reg_f, text="Longitud:").grid(row=2, column=0, sticky="w", pady=2)
        self.entry_lon = Entry(reg_f)
        self.entry_lon.grid(row=2, column=1, pady=2, sticky="ew")
        Label(reg_f, text="Tipo:").grid(row=3, column=0, sticky="w", pady=2)
        self.tipo_var = StringVar(value="Avistamiento")
        OptionMenu(reg_f, self.tipo_var, "Avistamiento", "Nido probable").grid(
            row=3, column=1, pady=2, sticky="ew"
        )
        Label(reg_f, text="Comentario:").grid(row=4, column=0, sticky="w", pady=2)
        self.entry_comentario = Entry(reg_f)
        self.entry_comentario.grid(row=4, column=1, pady=2, sticky="ew")
        Button(
            reg_f,
            text="Guardar Ubicación",
            command=self.guardar_ubicacion,
            bg="#4CAF50",
            fg="white",
        ).grid(row=5, column=0, columnspan=2, pady=10, sticky="ew")
        an_f = Frame(main, padx=10, pady=10, relief=tk.RIDGE, borderwidth=2)
        an_f.pack(pady=5, padx=5, fill=tk.X)
        Label(an_f, text="Análisis y Predicción", font=("Helvetica", 12, "bold")).grid(
            row=0, column=0, columnspan=3, pady=5
        )
        Button(
            an_f, text="Ver/Gestionar Mapa Completo", command=self.generar_mapa_completo
        ).grid(row=1, column=0, columnspan=3, pady=5, sticky="ew")
        Label(an_f, text="Nuevas predicciones:").grid(
            row=3, column=0, sticky="w", pady=5
        )
        self.entry_num_generar = Entry(an_f, width=5)
        self.entry_num_generar.grid(row=3, column=1, sticky="w")
        self.entry_num_generar.insert(0, "5")
        self.gen_btn = Button(
            an_f,
            text="Generar Predicciones Validadas",
            command=self.start_generation_thread,
            bg="#2196F3",
            fg="white",
        )
        self.gen_btn.grid(row=4, column=0, columnspan=3, pady=5, sticky="ew")
        self.status_lbl = Label(an_f, text="Listo.", fg="blue")
        self.status_lbl.grid(row=5, column=0, columnspan=3, pady=2)

    def guardar_ubicacion(self):
        lat_s, lon_s, tipo, com = (
            self.entry_lat.get(),
            self.entry_lon.get(),
            self.tipo_var.get(),
            self.entry_comentario.get(),
        )
        try:
            lat_f, lon_f = float(lat_s), float(lon_s)
            if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
                messagebox.showerror("Error", "Coordenadas fuera de rango.")
                return
        except ValueError:
            messagebox.showerror("Error", "Latitud y Longitud deben ser números.")
            return
        new_id = self._get_next_id()
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                [new_id, lat_f, lon_f, tipo, com, "N/A", "Registro manual"]
            )
        messagebox.showinfo("Éxito", f"Ubicación guardada con ID: {new_id}.")
        self.entry_lat.delete(0, tk.END)
        self.entry_lon.delete(0, tk.END)
        self.entry_comentario.delete(0, tk.END)

    def generar_mapa_completo(self):
        df = self.leer_datos()
        m = self.generar_mapa_base(df)  # Obtiene el mapa con las capas base

        if not df.empty:
            # Crea el clúster de marcadores y lo añade al mapa
            mc = MarkerCluster(name="Ubicaciones Registradas").add_to(m)
            for _, row in df.iterrows():
                uid = int(row["id"])
                score_info = f"<b>Puntuación:</b> {row.get('puntuacion', 'N/A')}<br>"
                reason_info = (
                    f"<b>Análisis:</b> {row.get('razon_validacion', 'N/A')}<br>"
                )
                popup_html = f"""<b>ID:</b> {uid}<br><b>Tipo:</b> {row["tipo"]}<br>
                <b>Coords:</b> ({row["lat"]:.5f}, {row["lon"]:.5f})<br>
                <b>Comentario:</b> {row.get("comentario", "N/A")}<hr>
                {"".join([score_info, reason_info]) if "Generado" in str(row["tipo"]) else ""}
                <a href="http://localhost:{SERVER_PORT}/delete?id={uid}" target="_blank" style="color:red;"><b>ELIMINAR</b></a>"""
                iframe = folium.IFrame(html=popup_html, width=300, height=180)
                popup = folium.Popup(iframe, max_width=300)
                color, icon = (
                    ("red", "home")
                    if row["tipo"] == "Nido probable"
                    else ("blue", "eye-open")
                    if row["tipo"] == "Avistamiento"
                    else ("green", "star")
                    if row["tipo"] == "Generado Potencial"
                    else ("purple", "question-sign")
                )
                folium.Marker(
                    [row["lat"], row["lon"]],
                    popup=popup,
                    tooltip=f"ID: {uid} - {row['tipo']}",
                    icon=folium.Icon(color=color, icon=icon, prefix="glyphicon"),
                ).add_to(mc)

        folium.LayerControl().add_to(m)

        map_path = os.path.realpath("mapa_gestion_aguilas.html")
        m.save(map_path)
        webbrowser.open(f"file://{map_path}")

    def generar_y_validar_ubicaciones_threaded(self, num_gen, q):
        df = self.leer_datos()
        nidos = df[df["tipo"] == "Nido probable"].copy()
        if len(nidos) < 2:
            q.put(("ERROR", "Se necesitan al menos 2 'Nidos probables'."))
            return
        nidos["weight"] = nidos["comentario"].apply(calculate_comment_weight)
        coords = nidos[["lat", "lon"]].to_numpy()
        weights = nidos["weight"].to_numpy()
        if np.sum(weights) == 0:
            q.put(("ERROR", "Pesos calculados son cero."))
            return
        mean, cov = (
            np.average(coords, axis=0, weights=weights),
            np.cov(coords, rowvar=False, aweights=weights) + np.eye(2) * 1e-5,
        )

        valid_points = []
        max_tries = num_gen * 30
        for i in range(max_tries):
            if len(valid_points) >= num_gen:
                break
            lat, lon = np.random.multivariate_normal(mean, cov, 1)[0]
            is_valid, reason, score = get_location_viability(lat, lon)
            q.put(
                (
                    "STATUS",
                    f"Intento {i + 1}/{max_tries}: ({lat:.3f}, {lon:.3f}) -> {reason}",
                )
            )
            if is_valid:
                valid_points.append(
                    {"lat": lat, "lon": lon, "score": score, "reason": reason}
                )
            time.sleep(0.2)

        valid_points.sort(key=lambda p: p["score"], reverse=True)
        q.put(("DONE", valid_points))

    def process_generation_queue(self):
        try:
            msg_type, data = self.generation_queue.get_nowait()
            if msg_type == "STATUS":
                self.status_lbl.config(text=data)
            elif msg_type == "ERROR":
                messagebox.showerror("Error", data)
                self.gen_btn.config(
                    state=tk.NORMAL, text="Generar Predicciones Validadas"
                )
                self.status_lbl.config(text="Error. Inténtelo de nuevo.")
            elif msg_type == "DONE":
                new_regs = []
                if data:
                    curr_id = self._get_next_id()
                    new_regs = [
                        [
                            curr_id + i,
                            p["lat"],
                            p["lon"],
                            "Generado Potencial",
                            "Validado ecológicamente",
                            p["score"],
                            p["reason"],
                        ]
                        for i, p in enumerate(data)
                    ]
                    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
                        csv.writer(f).writerows(new_regs)
                messagebox.showinfo(
                    "Completo",
                    f"Análisis finalizado. {len(new_regs)} ubicaciones viables guardadas.\nRegenere el mapa para verlas.",
                )
                self.gen_btn.config(
                    state=tk.NORMAL, text="Generar Predicciones Validadas"
                )
                self.status_lbl.config(
                    text=f"Proceso finalizado. {len(new_regs)} puntos añadidos."
                )
        except queue.Empty:
            pass
        self.root.after(100, self.process_generation_queue)

    def start_server(self):
        class Handler(http.server.SimpleHTTPRequestHandler):
            app = self

            def do_GET(self):
                p = urlparse(self.path)
                if p.path == "/delete":
                    try:
                        q = parse_qs(p.query)
                        id_del = int(q.get("id", [None])[0])
                        self.app.eliminar_ubicacion_por_id(id_del)
                        self.send_response(200)
                        self.send_header("Content-type", "text/html; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(
                            b"<html><body><p>Solicitud de eliminacion enviada. Puede cerrar esta pestana.</p></body></html>"
                        )
                    except (ValueError, IndexError, TypeError) as e:
                        self.send_error(400, f"Solicitud invalida: {e}")
                else:
                    self.send_error(404, "Pagina no encontrada")

        try:
            self.httpd = socketserver.TCPServer(("", SERVER_PORT), Handler)
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        except OSError as e:
            messagebox.showerror(
                "Error de Red",
                f"No se pudo iniciar servidor en puerto {SERVER_PORT}.\n{e}",
            )
            self.httpd = None
            self.root.destroy()

    def on_closing(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.root.destroy()

    def eliminar_ubicacion_por_id(self, id_del):
        try:
            df = pd.read_csv(CSV_FILE)
            if id_del not in df["id"].values:
                self.root.after(
                    0,
                    lambda: messagebox.showerror(
                        "Error", f"No se encontro ID {id_del}."
                    ),
                )
                return
            df = df[df["id"] != id_del]
            df.to_csv(CSV_FILE, index=False)
            self.root.after(0, lambda: self.ask_to_regenerate_map(id_del))
        except Exception as e:
            self.root.after(
                0, lambda e=e: messagebox.showerror("Error al eliminar", str(e))
            )

    def ask_to_regenerate_map(self, del_id):
        if messagebox.askyesno(
            "Exito",
            f"Ubicacion con ID {del_id} eliminada.\nDesea regenerar el mapa ahora?",
        ):
            self.generar_mapa_completo()

    def leer_datos(self):
        try:
            return pd.read_csv(CSV_FILE)
        except (FileNotFoundError, pd.errors.EmptyDataError):
            return pd.DataFrame(
                columns=[
                    "id",
                    "lat",
                    "lon",
                    "tipo",
                    "comentario",
                    "puntuacion",
                    "razon_validacion",
                ]
            )
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo leer CSV: {e}")
            return pd.DataFrame(
                columns=[
                    "id",
                    "lat",
                    "lon",
                    "tipo",
                    "comentario",
                    "puntuacion",
                    "razon_validacion",
                ]
            )

    # <<<--- FUNCIÓN generar_mapa_base MODIFICADA ---<<<
    def generar_mapa_base(self, df):
        if df.empty or df["lat"].isnull().all():
            map_center = [9.0, -80.0]
            zoom_start = 5
        else:
            map_center = [df["lat"].mean(), df["lon"].mean()]
            zoom_start = 6

        m = folium.Map(location=map_center, zoom_start=zoom_start, tiles=None)

        folium.TileLayer("OpenStreetMap", name="Estándar (Calles)").add_to(m)
        folium.TileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Esri",
            name="Satélite",
        ).add_to(m)
        folium.TileLayer(
            "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
            attr="OpenTopoMap",
            name="Topográfico",
        ).add_to(m)
        folium.TileLayer(
            "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
            attr="CartoDB",
            name="Modo Oscuro",
        ).add_to(m)

        folium.LatLngPopup().add_to(m)
        return m

    def start_generation_thread(self):
        try:
            num = int(self.entry_num_generar.get())
            if num <= 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror(
                "Error", "Numero de predicciones debe ser un entero positivo."
            )
            return
        self.gen_btn.config(state=tk.DISABLED, text="Analizando...")
        self.status_lbl.config(text=f"Iniciando analisis para {num} puntos...")
        threading.Thread(
            target=self.generar_y_validar_ubicaciones_threaded,
            args=(num, self.generation_queue),
            daemon=True,
        ).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    if app.root.winfo_exists():
        root.mainloop()
