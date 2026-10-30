# Installation et mises à niveau

[简体中文](installation.md) | [English](installation.en.md) | [Français](installation.fr.md)

[Retour à l’accueil](../README.fr.md)

### Télécharger l’installateur Windows

1. Ouvrez la [dernière version](https://github.com/KpTanl/OpenPictureStudio/releases/latest). Dans **Assets**, téléchargez `OpenPictureStudio-<version>-beta-setup.exe` et le fichier `.exe.sha256` du même nom.
2. Vérifiez le SHA256 de l’installateur, double-cliquez sur le fichier EXE et suivez l’assistant d’installation.
3. Lancez OpenPictureStudio, puis ouvrez une photo ou un dossier. La version installée n’exige ni Python ni Rust ; les modèles IA se téléchargent à la demande.

<details>
<summary>Comment vérifier le SHA256 ?</summary>

Dans le dossier de téléchargement, ouvrez PowerShell et exécutez les commandes ci-dessous, en remplaçant `<version>` dans le nom de fichier par la version réelle :

```powershell
Get-FileHash -LiteralPath '.\OpenPictureStudio-<version>-beta-setup.exe' -Algorithm SHA256
Get-Content -LiteralPath '.\OpenPictureStudio-<version>-beta-setup.exe.sha256'
```

Le hachage affiché par la première commande doit être identique à la valeur de 64 caractères au début de la sortie de la seconde. La valeur de contrôle sert à vérifier que le fichier téléchargé est complet et qu’il correspond au même fichier de la version publiée. Récupérez l’installateur et le fichier de contrôle dans les Releases de ce dépôt.

</details>

### Avertissement « Windows a protégé votre ordinateur » au premier lancement

L’installateur Windows actuel n’est pas signé numériquement. Au premier double-clic, Windows SmartScreen peut afficher « Windows a protégé votre ordinateur » et « Éditeur : inconnu ».

Une fois que vous avez confirmé que le fichier vient des [Releases de ce dépôt](https://github.com/KpTanl/OpenPictureStudio/releases/latest) et vérifié le SHA256 de la même version, cliquez sur **« Informations complémentaires » → « Exécuter quand même »** pour poursuivre l’installation. La capture d’exemple ci-dessous a déjà déplié « Informations complémentaires » :

<details>
<summary>Voir l’avertissement Windows du premier lancement</summary>

<img src="../.github/assets/readme/windows-smartscreen.zh-CN.png" width="480" alt="Avertissement Windows SmartScreen, avec en bas les boutons Exécuter quand même et Ne pas exécuter">

La version d’installateur de l’exemple est la 0.16.0 ; le nom du fichier change avec la version publiée. Si votre appareil ne propose pas « Exécuter quand même », suivez les indications du système ou la politique de votre organisation.

</details>

### Télécharger et installer avec PowerShell

Ouvrez un PowerShell ordinaire, dépliez la commande ci-dessous et **copiez-la entièrement avant de l’exécuter**. La commande fonctionne avec Windows PowerShell 5.1 et PowerShell 7 : elle télécharge la dernière version stable depuis ce dépôt, vérifie le SHA256, puis ouvre directement l’assistant d’installation.

Avant une mise à niveau, quittez OPS normalement. Les fichiers téléchargés restent dans le dossier temporaire indiqué par la commande, et l’installation ne redémarre pas l’ordinateur automatiquement. La protection de Windows et les politiques de l’organisation s’appliquent comme d’habitude.

<details>
<summary>Déplier la commande d’installation PowerShell</summary>

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $releasePage = 'https://github.com/KpTanl/OpenPictureStudio/releases/latest'
    try {
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/KpTanl/OpenPictureStudio/releases/latest' -Headers @{ 'User-Agent' = 'OpenPictureStudio-Install' } -TimeoutSec 30
        $name = 'OpenPictureStudio-{0}-beta-setup.exe' -f ($release.tag_name -replace '^v', '')
        $installer = @($release.assets | Where-Object { $_.name -ceq $name })
        $checksum = @($release.assets | Where-Object { $_.name -ceq "$name.sha256" })
        if ($installer.Count -ne 1 -or $checksum.Count -ne 1) { throw 'La version publiée ne contient pas exactement un installateur et un fichier de contrôle correspondants.' }

        $folder = Join-Path ([IO.Path]::GetTempPath()) ('OpenPictureStudio-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $folder | Out-Null
        Write-Host ('Dossier de téléchargement : ' + $folder)
        $exe = Join-Path $folder $name
        $sha = Join-Path $folder "$name.sha256"
        Invoke-WebRequest -UseBasicParsing -Uri $installer[0].browser_download_url -OutFile $exe -TimeoutSec 600
        Invoke-WebRequest -UseBasicParsing -Uri $checksum[0].browser_download_url -OutFile $sha -TimeoutSec 30

        $line = (Get-Content -LiteralPath $sha -Raw).Trim()
        if ($line -notmatch '^(?<hash>[a-fA-F0-9]{64})[ \t]+\*?(?<file>[^\r\n]+)$') { throw 'Fichier de contrôle invalide, ou correspondant à un autre installateur.' }
        $expected = $Matches['hash']
        if ($Matches['file'] -cne $name) { throw 'Fichier de contrôle invalide, ou correspondant à un autre installateur.' }
        if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ine $expected) { throw 'SHA256 différent : installateur non lancé.' }

        Write-Host 'SHA256 vérifié. Installateur en cours de lancement...'
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $exe
        $start.WorkingDirectory = $folder
        $start.UseShellExecute = $false
        $start.Arguments = '/NORESTART'
        $process = [System.Diagnostics.Process]::Start($start)
        $process.WaitForExit()
        switch ($process.ExitCode) {
            0 { Write-Host 'Installation terminée.' }
            { $_ -in 2, 5 } { Write-Host 'Installation annulée ou interrompue.' }
            default { throw ('Installation non terminée, code de sortie : ' + $process.ExitCode) }
        }
    } catch {
        throw ("Installation impossible. Page de la version : $releasePage`n" + $_.Exception.Message)
    }
}
```

</details>

### Mises à niveau et recherche de versions

La version installée recherche une version stable automatiquement, au plus une fois toutes les 24 heures. Vous pouvez désactiver cette recherche dans « Paramètres → Général », ou la lancer à la main dans la page « À propos ». Le programme ouvre la page de téléchargement, et c’est vous qui téléchargez et installez. La requête comprend la version de l’application et les informations HTTP nécessaires ; GitHub voit votre adresse IP et l’heure de la requête.

<details>
<summary>Installation par-dessus, réparation et premières versions de test</summary>

Avant une mise à niveau, quittez toutes les instances d’OpenPictureStudio en cours, puis lancez le nouvel installateur pour installer par-dessus. Une fois l’installation terminée, fermez l’installateur et relancez le programme. Vos paramètres, vos dossiers de données personnalisés, vos modèles et vos projets sont conservés.

En cas de conflit de dossier, suivez les indications de l’installateur : sauvegardez les fichiers en conflit et déplacez-les ailleurs. Si l’installation a été interrompue ou si une réparation est nécessaire, relancez l’installateur complet de la même version et suivez les indications pour terminer la réparation.

Les premières versions de test peuvent ne pas avoir de manifeste d’installation. Si l’ancien programme est encore là, sauvegardez d’abord vos données et désinstallez l’ancienne version ; si l’ancien dossier d’installation a été supprimé ou est complètement vide, vous pouvez réinstaller directement. Quand l’installateur refuse d’écraser des fichiers, il affiche l’ancien dossier d’installation.

</details>

## Composants d’interface et licence

Depuis la version 0.17.0, les composants d’interface sont passés de PyQt6 à PySide6. Les versions qui utilisent PySide6 fournissent les composants nécessaires dans l’installateur complet : il suffit d’installer par-dessus en suivant les étapes ci-dessus, sans installer séparément PySide6, Shiboken ni Qt.

PyQt6 est proposé sous GPLv3 et sous licence commerciale ; PySide6 est proposé sous des licences libres comme LGPLv3, avec une option de licence commerciale. Après la migration, le code original du projet reste sous licence MIT, tandis que les composants d’interface, les autres dépendances et les modèles suivent chacun leur propre licence. Les mentions de licence et les informations sur les sources correspondantes sont fournies avec le programme ; voir [Licences et composants tiers](licensing.fr.md#migration-de-pyqt6-vers-pyside6).
