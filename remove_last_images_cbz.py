import zipfile
import os
import shutil
import tempfile

def remove_last_images_from_cbz(cbz_path, num_to_remove=7):
    print(f"\nTraitement de : {cbz_path}")
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

def process_folder(folder_path, num_to_remove=7):
    folder_path = folder_path.strip().strip('"')
    if not os.path.isdir(folder_path):
        print("Ce dossier n'existe pas.")
        return

    cbz_files = [
        f for f in os.listdir(folder_path)
        if f.lower().endswith('.cbz')
    ]

    if not cbz_files:
        print("Aucun fichier .cbz trouvé dans ce dossier.")
        return

    print(f"\n📁 Dossier : {folder_path}")
    for cbz_file in cbz_files:
        full_path = os.path.join(folder_path, cbz_file)
        remove_last_images_from_cbz(full_path, num_to_remove)

# Lancement
if __name__ == "__main__":
    chemin_dossier = input("Chemin du dossier contenant les fichiers CBZ : ").strip()
    process_folder(chemin_dossier)
