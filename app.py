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

# Nombre del archivo donde se guardan los datos
CSV_FILE = 'ubicaciones_aguilas.csv'
# Puerto para el servidor local de eliminación
SERVER_PORT = 8080

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Gestor de Nidos de Águila Harpía")
        self.root.geometry("400x450")

        if not self.setup_csv():
            self.root.destroy()
            return
        
        self.httpd = None
        self.start_server()
        
        if self.httpd:
            self.create_widgets()
            self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_csv(self):
        EXPECTED_HEADER = ['id', 'lat', 'lon', 'tipo', 'comentario']
        if os.path.exists(CSV_FILE):
            try:
                with open(CSV_FILE, 'r', newline='', encoding='utf-8') as f:
                    header = next(csv.reader(f))
                if header == EXPECTED_HEADER:
                    return True
                backup_path = CSV_FILE + '.bak'
                os.rename(CSV_FILE, backup_path)
                messagebox.showwarning("Formato de Archivo Antiguo", f"Se detectó un CSV con formato antiguo. Sus datos se guardaron en:\n{backup_path}\n\nSe creará un nuevo archivo correcto.")
            except (StopIteration, IndexError): pass
            except Exception as e:
                messagebox.showerror("Error al leer CSV", f"No se pudo verificar el archivo CSV: {e}"); return False
        try:
            with open(CSV_FILE, 'w', newline='', encoding='utf-8') as file:
                csv.writer(file).writerow(EXPECTED_HEADER)
            return True
        except Exception as e:
            messagebox.showerror("Error al crear CSV", f"No se pudo crear el archivo CSV: {e}"); return False

    def _get_next_id(self):
        try:
            df = pd.read_csv(CSV_FILE)
            if df.empty: return 1
            return int(df['id'].max()) + 1
        except (FileNotFoundError, pd.errors.EmptyDataError):
            return 1

    def create_widgets(self):
        main_frame = Frame(self.root, padx=10, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        register_frame = Frame(main_frame, padx=10, pady=10, relief=tk.RIDGE, borderwidth=2)
        register_frame.pack(pady=5, padx=5, fill=tk.X)
        Label(register_frame, text="Registrar Nueva Ubicación", font=("Helvetica", 12, "bold")).grid(row=0, column=0, columnspan=2, pady=5)
        Label(register_frame, text="Latitud:").grid(row=1, column=0, sticky="w", pady=2)
        self.entry_lat = Entry(register_frame)
        self.entry_lat.grid(row=1, column=1, pady=2, sticky="ew")
        Label(register_frame, text="Longitud:").grid(row=2, column=0, sticky="w", pady=2)
        self.entry_lon = Entry(register_frame)
        self.entry_lon.grid(row=2, column=1, pady=2, sticky="ew")
        Label(register_frame, text="Tipo:").grid(row=3, column=0, sticky="w", pady=2)
        self.tipo_var = StringVar(value="Avistamiento")
        OptionMenu(register_frame, self.tipo_var, "Avistamiento", "Nido probable").grid(row=3, column=1, pady=2, sticky="ew")
        Label(register_frame, text="Comentario:").grid(row=4, column=0, sticky="w", pady=2)
        self.entry_comentario = Entry(register_frame)
        self.entry_comentario.grid(row=4, column=1, pady=2, sticky="ew")
        Button(register_frame, text="Guardar Ubicación", command=self.guardar_ubicacion, bg="#4CAF50", fg="white").grid(row=5, column=0, columnspan=2, pady=10, sticky="ew")

        analysis_frame = Frame(main_frame, padx=10, pady=10, relief=tk.RIDGE, borderwidth=2)
        analysis_frame.pack(pady=5, padx=5, fill=tk.X)
        Label(analysis_frame, text="Herramientas de Análisis", font=("Helvetica", 12, "bold")).grid(row=0, column=0, columnspan=3, pady=5)
        Button(analysis_frame, text="Ver/Gestionar Mapa Completo", command=self.generar_mapa_completo).grid(row=1, column=0, columnspan=3, pady=5, sticky="ew")
        
        Label(analysis_frame, text="Nuevas ubicaciones a generar:").grid(row=3, column=0, sticky="w", pady=5)
        self.entry_num_generar = Entry(analysis_frame, width=5)
        self.entry_num_generar.grid(row=3, column=1, sticky="w")
        self.entry_num_generar.insert(0, "10")
        Button(analysis_frame, text="Generar y Guardar Ubicaciones", command=self.generar_y_guardar_ubicaciones, bg="#2196F3", fg="white").grid(row=4, column=0, columnspan=3, pady=5, sticky="ew")

    def start_server(self):
        class RequestHandler(http.server.SimpleHTTPRequestHandler):
            app_instance = self
            def do_GET(self):
                parsed_path = urlparse(self.path)
                if parsed_path.path == '/delete':
                    try:
                        query = parse_qs(parsed_path.query)
                        id_to_delete = int(query.get('id', [None])[0])
                        if id_to_delete is not None:
                            self.app_instance.eliminar_ubicacion_por_id(id_to_delete)
                            self.send_response(200)
                            self.send_header("Content-type", "text/html; charset=utf-8")
                            self.end_headers()
                            html_respuesta = "<html><head><title>Eliminado</title></head><body><p>Solicitud de eliminación enviada. Puede cerrar esta pestaña.</p></body></html>"
                            self.wfile.write(html_respuesta.encode('utf-8'))
                        else: raise ValueError("ID no proporcionado")
                    except (ValueError, IndexError, TypeError) as e:
                        self.send_error(400, f"Solicitud inválida: {e}")
                else: self.send_error(404, "Página no encontrada")
        try:
            self.httpd = socketserver.TCPServer(("", SERVER_PORT), RequestHandler)
            self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.server_thread.start()
            print(f"Servidor de eliminación iniciado en el puerto {SERVER_PORT}")
        except OSError as e:
            messagebox.showerror("Error Crítico de Red", f"No se pudo iniciar el servidor en el puerto {SERVER_PORT}.\n\n{e}")
            self.httpd = None
            self.root.destroy()
        
    def on_closing(self):
        print("Cerrando la aplicación y el servidor...")
        if self.httpd: self.httpd.shutdown(); self.httpd.server_close()
        self.root.destroy()
        
    def eliminar_ubicacion_por_id(self, id_to_delete):
        try:
            df = pd.read_csv(CSV_FILE)
            if id_to_delete not in df['id'].values:
                self.root.after(0, lambda: messagebox.showerror("Error", f"No se encontró ID {id_to_delete}."))
                return
            df = df[df['id'] != id_to_delete]
            df.to_csv(CSV_FILE, index=False)
            self.root.after(0, lambda: self.ask_to_regenerate_map(id_to_delete))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error al eliminar", str(e)))

    def ask_to_regenerate_map(self, deleted_id):
        if messagebox.askyesno("Éxito", f"Ubicación con ID {deleted_id} eliminada.\n\n¿Desea regenerar el mapa ahora?"):
            self.generar_mapa_completo()

    def guardar_ubicacion(self):
        lat_str, lon_str, tipo, comentario = self.entry_lat.get(), self.entry_lon.get(), self.tipo_var.get(), self.entry_comentario.get()
        if not lat_str or not lon_str: messagebox.showerror("Error", "Latitud y Longitud no pueden estar vacíos."); return
        try:
            lat_f, lon_f = float(lat_str), float(lon_str)
            if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
                messagebox.showerror("Error de Validación", "Coordenadas fuera de rango."); return
        except ValueError: messagebox.showerror("Error", "Latitud y Longitud deben ser números válidos."); return
        nuevo_id = self._get_next_id()
        with open(CSV_FILE, 'a', newline='', encoding='utf-8') as file:
            csv.writer(file).writerow([nuevo_id, lat_f, lon_f, tipo, comentario])
        messagebox.showinfo("Éxito", f"Ubicación guardada con ID: {nuevo_id}."); 
        self.entry_lat.delete(0, tk.END); self.entry_lon.delete(0, tk.END); self.entry_comentario.delete(0, tk.END)

    # --- FUNCIÓN MODIFICADA ---
    def leer_datos(self):
        try:
            return pd.read_csv(CSV_FILE)
        except (FileNotFoundError, pd.errors.EmptyDataError):
            # Devuelve un DataFrame vacío con las columnas esperadas
            return pd.DataFrame(columns=['id', 'lat', 'lon', 'tipo', 'comentario'])
        except Exception as e:
            messagebox.showerror("Error de Lectura", f"No se pudo leer el archivo CSV: {e}")
            # En caso de otro error, también devuelve un DF vacío para evitar que la app se caiga
            return pd.DataFrame(columns=['id', 'lat', 'lon', 'tipo', 'comentario'])

    # --- FUNCIÓN MODIFICADA ---
    def generar_mapa_base(self, df):
        if df.empty:
            # Centro predeterminado (Panamá, hábitat del Águila Harpía) y zoom
            map_center = [9.0, -80.0]
            zoom_start = 5
        else:
            # Centra el mapa en el promedio de las coordenadas existentes
            map_center = [df['lat'].mean(), df['lon'].mean()]
            zoom_start = 6
        
        m = folium.Map(location=map_center, zoom_start=zoom_start, tiles="OpenStreetMap")
        folium.LatLngPopup().add_to(m)
        return m

    # --- FUNCIÓN MODIFICADA ---
    def generar_mapa_completo(self):
        df = self.leer_datos()
        m = self.generar_mapa_base(df)

        # Solo añade el clúster de marcadores si hay datos
        if not df.empty:
            marker_cluster = MarkerCluster(name="Ubicaciones Registradas").add_to(m)
            for _, row in df.iterrows():
                unique_id = int(row['id'])
                popup_html = f"""<b>ID:</b> {unique_id}<br><b>Tipo:</b> {row['tipo']}<br><b>Coords:</b> ({row['lat']:.5f}, {row['lon']:.5f})<br><b>Comentario:</b> {row.get('comentario', 'N/A')}<br><br><a href="http://localhost:{SERVER_PORT}/delete?id={unique_id}" target="_blank"><b>ELIMINAR</b></a>"""
                iframe = folium.IFrame(html=popup_html, width=280, height=150)
                popup = folium.Popup(iframe, max_width=280)
                color, icon = ('red', 'home') if row['tipo'] == 'Nido probable' else ('blue', 'eye-open') if row['tipo'] == 'Avistamiento' else ('green', 'star')
                folium.Marker([row['lat'], row['lon']], popup=popup, tooltip=f"ID: {unique_id} - {row['tipo']}", icon=folium.Icon(color=color, icon=icon, prefix='glyphicon')).add_to(marker_cluster)
            
            # Solo añade el control de capas si hay capas que controlar
            folium.LayerControl().add_to(m)

        map_path = os.path.realpath("mapa_gestion_aguilas.html")
        m.save(map_path)
        webbrowser.open(f"file://{map_path}")
        # Se elimina el messagebox de éxito para no ser molesto cuando solo se quiere ver
        
    def generar_y_guardar_ubicaciones(self):
        try: num_a_generar = int(self.entry_num_generar.get())
        except ValueError: messagebox.showerror("Error", "Número de ubicaciones debe ser un entero."); return
        
        df = self.leer_datos()
        nidos = df[df['tipo'] == 'Nido probable']
        if len(nidos) < 2: messagebox.showwarning("Datos Insuficientes", "Se necesitan al menos 2 'Nidos probables'."); return
        
        coords = nidos[['lat', 'lon']].to_numpy()
        mean_coords = np.mean(coords, axis=0)
        cov_matrix = np.cov(coords, rowvar=False) + np.eye(2) * 1e-6 
        generated_coords = np.random.multivariate_normal(mean_coords, cov_matrix, num_a_generar)
        
        current_id = self._get_next_id()
        nuevos_registros = [[current_id + i, lat, lon, "Generado Potencial", "Generado aut."] for i, (lat, lon) in enumerate(generated_coords)]
        
        with open(CSV_FILE, 'a', newline='', encoding='utf-8') as file:
            csv.writer(file).writerows(nuevos_registros)
        
        messagebox.showinfo("Generación Completa", f"Se han generado y GUARDADO {len(nuevos_registros)} nuevas ubicaciones.\nRegenere el mapa para verlas.")

if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    if app.root.winfo_exists():
        root.mainloop()