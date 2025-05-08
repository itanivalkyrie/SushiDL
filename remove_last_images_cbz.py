import zipfile
import os
import shutil
import tempfile

total_removed = 0  # compteur global

def remove_last_images_from_cbz(cbz_path, num_to_remove=7):
    global total_removed
    print(f"\n📂 Traitement de : {cbz_path}")
    if not cbz_path.lower().endswith('.cbz'):
        print("   ➤ Ignoré : ce n'est pas un fichier .cbz")
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            with zipfile.ZipFile(cbz_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
        except Exception as e:
            print(f"   ❌ Erreur lors de l'extraction : {e}")
            return

        image_files = sorted([
            f for f in os.listdir(temp_dir)
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
        ])

        if len(image_files) <= num_to_remove:
            print("   ⚠ Pas assez d'images à supprimer.")
            return

        for f in image_files[-num_to_remove:]:
            os.remove(os.path.join(temp_dir, f))
            total_removed += 1
            print(f"   🗑 Supprimé : {f}")

        backup_path = cbz_path + '.bak'
        shutil.move(cbz_path, backup_path)

        with zipfile.ZipFile(cbz_path, 'w', compression=zipfile.ZIP_DEFLATED) as zip_out:
            for root, _, files in os.walk(temp_dir):
                for file in sorted(files):
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, temp_dir)
                    zip_out.write(file_path, arcname)

        print("   ✅ Nouveau CBZ créé (sauvegarde .bak faite).")

def process_path(path_input, num_to_remove=7):
    path_input = path_input.strip().strip('"')
    if not os.path.exists(path_input):
        print("❌ Le chemin fourni n'existe pas.")
        return

    if os.path.isfile(path_input) and path_input.lower().endswith('.cbz'):
        remove_last_images_from_cbz(path_input, num_to_remove)
    elif os.path.isdir(path_input):
        cbz_files = [f for f in os.listdir(path_input) if f.lower().endswith('.cbz')]
        if not cbz_files:
            print("❌ Aucun fichier .cbz trouvé dans ce dossier.")
            return

        print(f"\n📁 Dossier : {path_input}")
        for cbz_file in cbz_files:
            full_path = os.path.join(path_input, cbz_file)
            remove_last_images_from_cbz(full_path, num_to_remove)
    else:
        print("❌ Le chemin n'est ni un fichier .cbz ni un dossier valide.")

# === Point d'entrée ===
if __name__ == "__main__":
    print("🧹 Nettoyeur de fichiers CBZ – Suppression d'images finales\n")

    while True:
        try:
            nb = input("🔢 Combien d'images supprimer à la fin ? (défaut : 7) : ").strip()
            num_to_remove = int(nb) if nb else 7
            if num_to_remove < 1:
                raise ValueError
        except ValueError:
            print("❌ Nombre invalide. Utilisation de la valeur par défaut : 7")
            num_to_remove = 7

        chemin = input("📂 Glissez un fichier .cbz ou un dossier contenant des .cbz : ").strip()
        process_path(chemin, num_to_remove)

        print(f"\n✅ Total : {total_removed} image(s) supprimée(s).\n")
        again = input("🔁 Voulez-vous traiter un autre fichier/dossier ? (o/n) : ").strip().lower()
        if again not in ['o', 'oui', 'y', 'yes']:
            print("👋 Fin du programme. Merci d'avoir utilisé ce script.")
            break