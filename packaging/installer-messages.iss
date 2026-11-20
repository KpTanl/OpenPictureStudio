; Project text for the installer and uninstaller. Every key exists in all three languages.
; Scripts read these through CustomMessage, Fail and FailWith; %1 and %2 are filled in by the caller, %n is a line break.

[CustomMessages]
en.OpsBusy=Another installation or removal is in progress.
zh_CN.OpsBusy=另一个安装或卸载正在进行。
fr.OpsBusy=Une autre installation ou désinstallation est en cours.

en.OpsProtectionInspect=Cannot inspect installation protection. Close other installers and try again.
zh_CN.OpsProtectionInspect=无法检查安装保护。请关闭其他安装程序后重试。
fr.OpsProtectionInspect=Impossible de vérifier la protection de l'installation. Fermez les autres programmes d'installation, puis réessayez.

en.OpsProtectionCreate=Cannot create installation protection.
zh_CN.OpsProtectionCreate=无法创建安装保护。
fr.OpsProtectionCreate=Impossible de créer la protection de l'installation.

en.OpsProtectionPermissions=Cannot set installation protection permissions.
zh_CN.OpsProtectionPermissions=无法设置安装保护的权限。
fr.OpsProtectionPermissions=Impossible de définir les autorisations de la protection de l'installation.

en.OpsProtectionWait=Cannot obtain installation protection. Try again.
zh_CN.OpsProtectionWait=无法获得安装保护。请重试。
fr.OpsProtectionWait=Impossible d'obtenir la protection de l'installation. Réessayez.

en.OpsRunning=OpenPictureStudio or an earlier version is running. Close all instances, including other signed-in users, then try again.
zh_CN.OpsRunning=OpenPictureStudio 或其早期版本正在运行。请关闭所有实例（包括其他已登录用户的实例）后重试。
fr.OpsRunning=OpenPictureStudio ou une version antérieure est en cours d'exécution. Fermez toutes les instances, y compris celles des autres utilisateurs connectés, puis réessayez.

en.OpsRunningUnknown=Cannot check running applications. No files have been changed.
zh_CN.OpsRunningUnknown=无法检查正在运行的程序。没有改动任何文件。
fr.OpsRunningUnknown=Impossible de vérifier les applications en cours d'exécution. Aucun fichier n'a été modifié.

en.OpsInspect=Cannot inspect: %1
zh_CN.OpsInspect=无法检查：%1
fr.OpsInspect=Impossible d'examiner : %1

en.OpsLinked=A linked path cannot be updated: %1
zh_CN.OpsLinked=链接路径无法更新：%1
fr.OpsLinked=Un chemin lié ne peut pas être mis à jour : %1

en.OpsVersionUnknown=Unrecognized installed version: %1
zh_CN.OpsVersionUnknown=无法识别已安装的版本：%1
fr.OpsVersionUnknown=Version installée non reconnue : %1

en.OpsManifestDamaged=Missing or damaged installation manifest. Back up your data and use a clean installation.
zh_CN.OpsManifestDamaged=安装清单缺失或损坏。请备份资料后重新安装。
fr.OpsManifestDamaged=Manifeste d'installation absent ou endommagé. Sauvegardez vos données et effectuez une nouvelle installation.

en.OpsManifestUnsupported=Unsupported installation manifest.
zh_CN.OpsManifestUnsupported=不支持这个安装清单。
fr.OpsManifestUnsupported=Manifeste d'installation non pris en charge.

en.OpsManifestEntry=Invalid manifest entry.
zh_CN.OpsManifestEntry=安装清单中有无效条目。
fr.OpsManifestEntry=Entrée de manifeste non valide.

en.OpsManifestPath=Unsafe manifest path: %1
zh_CN.OpsManifestPath=安装清单中的路径不安全：%1
fr.OpsManifestPath=Chemin de manifeste non sûr : %1

en.OpsManifestDuplicate=Duplicate manifest path: %1
zh_CN.OpsManifestDuplicate=安装清单中的路径重复：%1
fr.OpsManifestDuplicate=Chemin de manifeste en double : %1

en.OpsManifestNoProgram=The program executable is absent from the manifest.
zh_CN.OpsManifestNoProgram=安装清单中没有主程序文件。
fr.OpsManifestNoProgram=L'exécutable du programme est absent du manifeste.

en.OpsManifestVersion=The installed version and manifest do not match.
zh_CN.OpsManifestVersion=已安装的版本与安装清单不一致。
fr.OpsManifestVersion=La version installée et le manifeste ne correspondent pas.

en.OpsManifestUseRepair=The installation manifest is missing or damaged. Run the installer for the installed version and choose Repair.
zh_CN.OpsManifestUseRepair=安装清单缺失或损坏。请运行与已安装版本相同的安装包，并选择查漏安装。
fr.OpsManifestUseRepair=Le manifeste d'installation est absent ou endommagé. Exécutez le programme d'installation de la version installée et choisissez Réparation.

en.OpsManifestMissing=This installation has no manifest. Run the installer for the installed version and choose Repair. For an older beta, back up your data and uninstall it manually. Directory: %1
zh_CN.OpsManifestMissing=这个安装没有安装清单。请运行与已安装版本相同的安装包，并选择查漏安装。如果是早期测试版，请备份资料后手动卸载。目录：%1
fr.OpsManifestMissing=Cette installation n'a pas de manifeste. Exécutez le programme d'installation de la version installée et choisissez Réparation. Pour une ancienne bêta, sauvegardez vos données et désinstallez-la manuellement. Dossier : %1

en.OpsManifestReadNew=Cannot read the new installation manifest.
zh_CN.OpsManifestReadNew=无法读取新的安装清单。
fr.OpsManifestReadNew=Impossible de lire le nouveau manifeste d'installation.

en.OpsRepairNeedsSameVersion=The installation manifest is missing or damaged. Run the installer for version %1 and choose Repair.
zh_CN.OpsRepairNeedsSameVersion=安装清单缺失或损坏。请运行 %1 版本的安装包，并选择查漏安装。
fr.OpsRepairNeedsSameVersion=Le manifeste d'installation est absent ou endommagé. Exécutez le programme d'installation de la version %1 et choisissez Réparation.

en.OpsTwoModes=Two installation modes share this directory. Back up your data and separate the installations first.
zh_CN.OpsTwoModes=两种安装模式共用了这个目录。请先备份资料并分开这两个安装。
fr.OpsTwoModes=Deux modes d'installation partagent ce dossier. Sauvegardez vos données et séparez d'abord les installations.

en.OpsIdentity=Cannot identify the existing installation.
zh_CN.OpsIdentity=无法识别已有的安装。
fr.OpsIdentity=Impossible d'identifier l'installation existante.

en.OpsDirectoryMismatch=The selected directory does not match the existing installation.
zh_CN.OpsDirectoryMismatch=所选目录与已有的安装不一致。
fr.OpsDirectoryMismatch=Le dossier sélectionné ne correspond pas à l'installation existante.

en.OpsUninstallerLocation=Invalid existing uninstaller location.
zh_CN.OpsUninstallerLocation=现有卸载程序的位置无效。
fr.OpsUninstallerLocation=Emplacement du programme de désinstallation existant non valide.

en.OpsPathTooLong=This installation path is too long. Use a path of %1 characters or fewer.
zh_CN.OpsPathTooLong=安装路径太长。请使用不超过 %1 个字符的路径。
fr.OpsPathTooLong=Ce chemin d'installation est trop long. Utilisez un chemin de %1 caractères au maximum.

en.OpsUnknownDirectory=Back up and move this unrecognized directory before continuing: %1
zh_CN.OpsUnknownDirectory=发现无法识别的目录。请备份并移走后继续：%1
fr.OpsUnknownDirectory=Sauvegardez et déplacez ce dossier non reconnu avant de continuer : %1

en.OpsUnknownFile=Back up and move this unrecognized file before continuing: %1
zh_CN.OpsUnknownFile=发现无法识别的文件。请备份并移走后继续：%1
fr.OpsUnknownFile=Sauvegardez et déplacez ce fichier non reconnu avant de continuer : %1

en.OpsModifiedFile=This program file has been modified. Back it up and move it before continuing, or choose Repair in the installer: %1
zh_CN.OpsModifiedFile=这个程序文件已被改动。请备份并移走后继续，或在安装程序中选择查漏安装：%1
fr.OpsModifiedFile=Ce fichier du programme a été modifié. Sauvegardez-le et déplacez-le avant de continuer, ou choisissez Réparation dans le programme d'installation : %1

en.OpsAskRepair=%1 program file(s) are damaged or modified, for example:%n%2%n%nSwitch to Repair and replace them?
zh_CN.OpsAskRepair=发现 %1 个程序文件已损坏或被改动，例如：%n%2%n%n是否改用查漏安装替换这些文件？
fr.OpsAskRepair=%1 fichier(s) du programme sont endommagés ou modifiés, par exemple :%n%2%n%nPasser en mode Réparation pour les remplacer ?

en.OpsRepairDeclined=Installation stopped. No program files have been changed.
zh_CN.OpsRepairDeclined=已停止安装。程序文件没有改动。
fr.OpsRepairDeclined=Installation arrêtée. Aucun fichier du programme n'a été modifié.

en.OpsTreeChanged=Program files changed during the check. Close other programs and run this installer again: %1
zh_CN.OpsTreeChanged=核对期间程序文件发生了变化。请关闭其他程序后重新运行安装程序：%1
fr.OpsTreeChanged=Des fichiers du programme ont changé pendant la vérification. Fermez les autres programmes et relancez ce programme d'installation : %1

en.OpsTreeCount=Program files changed during the check. Close other programs and run this installer again.
zh_CN.OpsTreeCount=核对期间程序文件发生了变化。请关闭其他程序后重新运行安装程序。
fr.OpsTreeCount=Des fichiers du programme ont changé pendant la vérification. Fermez les autres programmes et relancez ce programme d'installation.

en.OpsVerificationTitle=Checking installation
zh_CN.OpsVerificationTitle=正在检查安装
fr.OpsVerificationTitle=Vérification de l'installation

en.OpsVerificationDescription=Existing files are being verified.
zh_CN.OpsVerificationDescription=正在核对已有文件。
fr.OpsVerificationDescription=Les fichiers existants sont en cours de vérification.

en.OpsVerifying=Verifying program files
zh_CN.OpsVerifying=正在核对程序文件
fr.OpsVerifying=Vérification des fichiers du programme

en.OpsVerifyingNew=Verifying installed files
zh_CN.OpsVerifyingNew=正在核对已写入的文件
fr.OpsVerifyingNew=Vérification des fichiers installés

en.OpsVerificationCancelled=Verification cancelled. No program files have been changed.
zh_CN.OpsVerificationCancelled=已取消核对。程序文件没有改动。
fr.OpsVerificationCancelled=Vérification annulée. Aucun fichier du programme n'a été modifié.

en.OpsVerificationFailed=Program verification failed. Run this installer again to repair: %1
zh_CN.OpsVerificationFailed=程序文件校验失败。请再次运行此安装程序修复：%1
fr.OpsVerificationFailed=La vérification du programme a échoué. Relancez ce programme d'installation pour réparer : %1

en.OpsInspectOldDirectory=Cannot inspect the old installation directory: %1
zh_CN.OpsInspectOldDirectory=无法检查旧的安装目录：%1
fr.OpsInspectOldDirectory=Impossible d'examiner l'ancien dossier d'installation : %1

en.OpsRecoveryRead=Cannot read installation recovery record.
zh_CN.OpsRecoveryRead=无法读取安装恢复记录。
fr.OpsRecoveryRead=Impossible de lire l'enregistrement de reprise de l'installation.

en.OpsRecoveryDamaged=Damaged installation recovery record.
zh_CN.OpsRecoveryDamaged=安装恢复记录已损坏。
fr.OpsRecoveryDamaged=L'enregistrement de reprise de l'installation est endommagé.

en.OpsRecoveryMismatch=Run the same installer in the original installation mode to repair this installation.
zh_CN.OpsRecoveryMismatch=请用原来的安装模式运行同一个安装程序来修复这个安装。
fr.OpsRecoveryMismatch=Exécutez le même programme d'installation dans le mode d'origine pour réparer cette installation.

en.OpsStorageRead=Cannot read storage configuration.
zh_CN.OpsStorageRead=无法读取存储配置。
fr.OpsStorageRead=Impossible de lire la configuration du stockage.

en.OpsStorageInvalid=Invalid storage configuration.
zh_CN.OpsStorageInvalid=存储配置无效。
fr.OpsStorageInvalid=Configuration du stockage non valide.

en.OpsStorageConflict=Move the conflicting storage location or cancel its pending migration first: %1
zh_CN.OpsStorageConflict=存储位置与安装目录冲突。请先移走它，或取消待完成的迁移：%1
fr.OpsStorageConflict=Déplacez d'abord l'emplacement de stockage en conflit ou annulez sa migration en attente : %1

en.OpsMetadataConflict=Move the conflicting metadata file before retrying: %1
zh_CN.OpsMetadataConflict=安装元数据文件有冲突。请移走后重试：%1
fr.OpsMetadataConflict=Déplacez le fichier de métadonnées en conflit avant de réessayer : %1

en.OpsMetadataSave=Cannot save installation metadata.
zh_CN.OpsMetadataSave=无法保存安装元数据。
fr.OpsMetadataSave=Impossible d'enregistrer les métadonnées de l'installation.

en.OpsMetadataPublish=Cannot publish installation metadata: %1
zh_CN.OpsMetadataPublish=无法写入安装元数据：%1
fr.OpsMetadataPublish=Impossible de publier les métadonnées de l'installation : %1

en.OpsReadyNew=New installation: %1
zh_CN.OpsReadyNew=全新安装：%1
fr.OpsReadyNew=Nouvelle installation : %1

en.OpsReadyRepair=Repair installation: %1
zh_CN.OpsReadyRepair=修复安装：%1
fr.OpsReadyRepair=Réparation de l'installation : %1

en.OpsReadyUpgrade=Update: %1 -> %2
zh_CN.OpsReadyUpgrade=升级：%1 -> %2
fr.OpsReadyUpgrade=Mise à jour : %1 -> %2

en.OpsReadyStandard=Method: Standard update
zh_CN.OpsReadyStandard=覆盖方式：标准覆盖
fr.OpsReadyStandard=Mode : Mise à jour standard

en.OpsReadyThorough=Method: Repair
zh_CN.OpsReadyThorough=覆盖方式：查漏安装
fr.OpsReadyThorough=Mode : Réparation

en.OpsVersionMismatch=The installer and program versions do not match.
zh_CN.OpsVersionMismatch=安装程序与程序文件的版本不一致。
fr.OpsVersionMismatch=Les versions du programme d'installation et du programme ne correspondent pas.

en.OpsSilentDowngrade=Silent downgrades are not supported.
zh_CN.OpsSilentDowngrade=静默安装不支持降级。
fr.OpsSilentDowngrade=Les rétrogradations silencieuses ne sont pas prises en charge.

en.OpsDowngradeConfirm=Installed: %1%nContinue with older version %2?%nBack up your data first. Newer projects and data may not open in the older version.
zh_CN.OpsDowngradeConfirm=已安装：%1%n要继续安装较旧的版本 %2 吗？%n请先备份资料。较新的项目和数据可能无法在旧版本中打开。
fr.OpsDowngradeConfirm=Version installée : %1%nContinuer avec la version antérieure %2 ?%nSauvegardez d'abord vos données. Les projets et données récents risquent de ne pas s'ouvrir dans l'ancienne version.

en.OpsDowngradeCancelled=Downgrade cancelled. No program files have been changed.
zh_CN.OpsDowngradeCancelled=已取消降级。程序文件没有改动。
fr.OpsDowngradeCancelled=Rétrogradation annulée. Aucun fichier du programme n'a été modifié.

en.OpsPreflightIncomplete=Installation preflight has not completed.
zh_CN.OpsPreflightIncomplete=安装前检查尚未完成。
fr.OpsPreflightIncomplete=Les vérifications préalables à l'installation ne sont pas terminées.

en.OpsVersionChanged=The installed version has changed. Restart this installer.
zh_CN.OpsVersionChanged=已安装的版本发生了变化。请重新启动此安装程序。
fr.OpsVersionChanged=La version installée a changé. Redémarrez ce programme d'installation.

en.OpsCreateDirectory=Cannot create the program directory.
zh_CN.OpsCreateDirectory=无法创建程序目录。
fr.OpsCreateDirectory=Impossible de créer le dossier du programme.

en.OpsOldFileChanged=Old program file has changed: %1
zh_CN.OpsOldFileChanged=旧的程序文件已被改动：%1
fr.OpsOldFileChanged=Un ancien fichier du programme a été modifié : %1

en.OpsRemoveOld=Cannot remove the old program file. Run this installer again: %1
zh_CN.OpsRemoveOld=无法删除旧的程序文件。请再次运行此安装程序：%1
fr.OpsRemoveOld=Impossible de supprimer l'ancien fichier du programme. Relancez ce programme d'installation : %1

en.OpsFinishFailed=Cannot finish installation. Run this installer again.
zh_CN.OpsFinishFailed=无法完成安装。请再次运行此安装程序。
fr.OpsFinishFailed=Impossible de terminer l'installation. Relancez ce programme d'installation.

en.OpsFinished=Installation is complete. Close this installer before starting OpenPictureStudio.
zh_CN.OpsFinished=安装已完成。请关闭此安装程序后再启动 OpenPictureStudio。
fr.OpsFinished=L'installation est terminée. Fermez ce programme d'installation avant de démarrer OpenPictureStudio.

en.OpsUnfinishedTitle=Installation did not finish
zh_CN.OpsUnfinishedTitle=安装没有完成
fr.OpsUnfinishedTitle=L'installation n'est pas terminée

en.OpsUnfinished=Program files are not complete. Run this installer again to repair them.
zh_CN.OpsUnfinished=程序文件不完整。请再次运行此安装程序修复。
fr.OpsUnfinished=Les fichiers du programme sont incomplets. Relancez ce programme d'installation pour les réparer.

en.OpsUninstallRepairFirst=Repair the incomplete installation before removing it.
zh_CN.OpsUninstallRepairFirst=请先修复未完成的安装，然后再卸载。
fr.OpsUninstallRepairFirst=Réparez l'installation incomplète avant de la supprimer.

en.OpsUninstallVerify=Cannot verify installed files. Repair the installation first.
zh_CN.OpsUninstallVerify=无法核对已安装的文件。请先修复安装。
fr.OpsUninstallVerify=Impossible de vérifier les fichiers installés. Réparez d'abord l'installation.

en.OpsModeTitle=Installation method
zh_CN.OpsModeTitle=覆盖方式
fr.OpsModeTitle=Mode d'installation

en.OpsModeDescription=Choose how to update the existing installation.
zh_CN.OpsModeDescription=选择如何处理已有的安装。
fr.OpsModeDescription=Choisissez comment mettre à jour l'installation existante.

en.OpsModePrompt=OpenPictureStudio is already installed.
zh_CN.OpsModePrompt=已安装 OpenPictureStudio。
fr.OpsModePrompt=OpenPictureStudio est déjà installé.

en.OpsModeStandard=Standard update: writes only new, changed, or missing files. Asks first if a program file has been modified.
zh_CN.OpsModeStandard=标准覆盖：只写入有变化或缺失的文件。发现程序文件被改动时先询问。
fr.OpsModeStandard=Mise à jour standard : écrit uniquement les fichiers nouveaux, modifiés ou manquants. Demande d'abord si un fichier du programme a été altéré.

en.OpsModeRepair=Repair: for an incomplete program. Checks each program file and replaces missing, damaged, or modified files. Program files changed by hand will be replaced.
zh_CN.OpsModeRepair=查漏安装：程序不完整时使用。逐个核对并替换缺失、损坏或被改动的程序文件。手动改过的程序文件会被替换。
fr.OpsModeRepair=Réparation : pour un programme incomplet. Vérifie chaque fichier du programme et remplace ceux qui sont manquants, endommagés ou modifiés. Les fichiers du programme modifiés à la main seront remplacés.

en.OpsCloseTitle=Close OpenPictureStudio
zh_CN.OpsCloseTitle=关闭 OpenPictureStudio
fr.OpsCloseTitle=Fermer OpenPictureStudio

en.OpsCloseDescription=OpenPictureStudio must close before files are updated.
zh_CN.OpsCloseDescription=更新文件前需要关闭 OpenPictureStudio。
fr.OpsCloseDescription=OpenPictureStudio doit être fermé avant la mise à jour des fichiers.

en.OpsCloseHint=Handle any save prompts in OpenPictureStudio. Exports and batch jobs in progress will be cancelled. Installation continues after the program exits.
zh_CN.OpsCloseHint=请在 OpenPictureStudio 中处理保存提示。正在进行的导出和批处理会被取消。程序退出后将继续安装。
fr.OpsCloseHint=Traitez les demandes d'enregistrement dans OpenPictureStudio. Les exportations et traitements par lots en cours seront annulés. L'installation reprend une fois le programme fermé.

en.OpsCloseDetails=Running processes: %1%nInstallation folder: %2
zh_CN.OpsCloseDetails=运行中的进程：%1%n安装位置：%2
fr.OpsCloseDetails=Processus en cours : %1%nDossier d'installation : %2

en.OpsCloseButton=Close program and continue
zh_CN.OpsCloseButton=关闭程序并继续
fr.OpsCloseButton=Fermer le programme et continuer

en.OpsCloseAgainButton=Request close again
zh_CN.OpsCloseAgainButton=重新请求关闭
fr.OpsCloseAgainButton=Redemander la fermeture

en.OpsRecheckButton=Check again
zh_CN.OpsRecheckButton=重新检查
fr.OpsRecheckButton=Vérifier à nouveau

en.OpsForceButton=End background processes
zh_CN.OpsForceButton=结束后台进程
fr.OpsForceButton=Terminer les processus en arrière-plan

en.OpsCloseWaiting=If the program does not respond, switch to it, close any open dialog, then click Request close again.
zh_CN.OpsCloseWaiting=如果程序没有反应，请切换到程序，关闭打开的对话框，然后点“重新请求关闭”。
fr.OpsCloseWaiting=Si le programme ne répond pas, passez-y, fermez toute boîte de dialogue ouverte, puis cliquez sur Redemander la fermeture.

en.OpsCloseSlow=The program is still running. Handle the save prompt or wait for it to exit, then check again.
zh_CN.OpsCloseSlow=程序仍在运行。请处理保存提示或完成退出后重新检查。
fr.OpsCloseSlow=Le programme est toujours en cours d'exécution. Traitez la demande d'enregistrement ou attendez sa fermeture, puis vérifiez à nouveau.

en.OpsCloseDenied=The program runs with higher privileges and cannot be asked to close. Exit it manually, then check again.
zh_CN.OpsCloseDenied=程序以更高权限运行，无法请求关闭。请手动退出，然后重新检查。
fr.OpsCloseDenied=Le programme s'exécute avec des privilèges plus élevés et ne peut pas recevoir de demande de fermeture. Quittez-le manuellement, puis vérifiez à nouveau.

en.OpsCloseNoWindow=The program is running without a window. It may be starting or finishing its exit, so it cannot be ended here. Please wait. If it does not end, use Task Manager or restart the computer, then run this installer again.
zh_CN.OpsCloseNoWindow=程序正在运行，但没有窗口。它可能正在启动或退出收尾，所以这里不能结束它。请稍候。长时间没有结束时，请用任务管理器结束它，或重启电脑后再运行安装程序。
fr.OpsCloseNoWindow=Le programme s'exécute sans fenêtre. Il est peut-être en train de démarrer ou de se fermer ; il ne peut donc pas être arrêté ici. Patientez. S'il ne se ferme pas, utilisez le Gestionnaire des tâches ou redémarrez l'ordinateur, puis relancez ce programme d'installation.

en.OpsCloseOther=OpenPictureStudio is running from another location or under another account. Close it normally, then check again.
zh_CN.OpsCloseOther=另一个位置或其他账户的 OpenPictureStudio 正在运行。请先正常关闭，然后重新检查。
fr.OpsCloseOther=OpenPictureStudio s'exécute depuis un autre emplacement ou sous un autre compte. Fermez-le normalement, puis vérifiez à nouveau.

en.OpsCloseOrphan=The program has exited, but %1 plugin process(es) are still running.
zh_CN.OpsCloseOrphan=程序已经退出，但还有 %1 个插件进程没有结束。
fr.OpsCloseOrphan=Le programme s'est fermé, mais %1 processus de module externe sont toujours en cours d'exécution.

en.OpsCloseClear=OpenPictureStudio is no longer running.
zh_CN.OpsCloseClear=OpenPictureStudio 已不在运行。
fr.OpsCloseClear=OpenPictureStudio n'est plus en cours d'exécution.

en.OpsForceConfirm=End %1 plugin process(es) by force? Results of their unfinished tasks will be lost.
zh_CN.OpsForceConfirm=要强制结束 %1 个插件进程吗？这些任务未完成的结果会丢失。
fr.OpsForceConfirm=Terminer de force %1 processus de module externe ? Les résultats de leurs tâches inachevées seront perdus.

en.OpsForceFailed=Some processes could not be ended. Exit them manually or restart the computer, then try again.
zh_CN.OpsForceFailed=有进程无法结束。请手动退出或重启电脑后重试。
fr.OpsForceFailed=Certains processus n'ont pas pu être terminés. Quittez-les manuellement ou redémarrez l'ordinateur, puis réessayez.
