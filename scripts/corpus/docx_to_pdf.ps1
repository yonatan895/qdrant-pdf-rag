<#
.SYNOPSIS
    Convert Word documents (.docx, .doc) to PDFs with heading bookmarks for the corpus (#1277).

.DESCRIPTION
    Runs on a Windows machine inside the gap that has Microsoft Word. For each
    .docx or .doc file in -SourceDir it writes <file stem>.pdf to -OutDir through
    Word's ExportAsFixedFormat: heading bookmarks (wdExportCreateHeadingBookmarks),
    document structure tags, print optimisation, and the document properties
    (IncludeDocProps), which carry the Title property into the PDF /Title.
    The file stem becomes the doc id, the Title the citation title, and the
    Heading 1-3 outline the heading path (docs/install_and_ops.md, "Adding
    documents to the corpus").

    Source documents are opened read-only and closed without saving. Word is
    quit in a finally block. Per file it warns about: no Heading 1-3 style in
    use, an empty Title, a Title containing a comma, and a stem that is not a
    usable doc id. Exit codes: 0 all converted, 1 some files failed or were
    skipped, 3 Word is not available.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File docx_to_pdf.ps1 -SourceDir D:\docs\word -OutDir D:\docs\pdf
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [switch]$Force
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

# Word object model constants.
$wdExportFormatPDF = 17
$wdExportOptimizeForPrint = 0
$wdExportAllDocument = 0
$wdExportDocumentContent = 0
$wdExportCreateHeadingBookmarks = 1
$wdDoNotSaveChanges = 0
$wdAlertsNone = 0
$wdFindStop = 0
# WdBuiltinStyle ids for Heading 1-3: language-independent, unlike style names.
$HeadingStyleIds = @(-2, -3, -4)
# The citation shape for a filename-stem doc id (src/mainframe_rag/agent/cites.py).
$StemPattern = '^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+$'

function Get-DocumentTitle($Document) {
    # BuiltInDocumentProperties needs late binding from PowerShell.
    $flags = [System.Reflection.BindingFlags]::GetProperty
    $props = $Document.BuiltInDocumentProperties
    $item = [System.__ComObject].InvokeMember('Item', $flags, $null, $props, @('Title'))
    $value = [System.__ComObject].InvokeMember('Value', $flags, $null, $item, $null)
    if ($null -eq $value) { return '' }
    return ([string]$value).Trim()
}

function Test-HeadingStyleUsed($Document) {
    foreach ($styleId in $HeadingStyleIds) {
        $find = $Document.Content.Find
        $find.ClearFormatting()
        $find.Text = ''
        $find.Format = $true
        $find.Forward = $true
        $find.Wrap = $wdFindStop
        $find.Style = $Document.Styles.Item($styleId)
        if ($find.Execute()) { return $true }
    }
    return $false
}

$source = (Resolve-Path -LiteralPath $SourceDir).Path
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$target = (Resolve-Path -LiteralPath $OutDir).Path

$files = @(Get-ChildItem -LiteralPath $source -File |
    Where-Object { ($_.Extension -eq '.docx' -or $_.Extension -eq '.doc') -and -not $_.Name.StartsWith('~$') } |
    Sort-Object Name)
if ($files.Count -eq 0) {
    Write-Warning "no .docx or .doc files in $source"
    exit 1
}

try {
    $word = New-Object -ComObject Word.Application
} catch {
    [Console]::Error.WriteLine('docx_to_pdf: Microsoft Word is not installed or cannot be started on this machine; run the script where Word is available.')
    exit 3
}

$failed = 0
try {
    $word.Visible = $false
    $word.DisplayAlerts = $wdAlertsNone
    foreach ($group in ($files | Group-Object { $_.BaseName.ToLowerInvariant() })) {
        if ($group.Count -gt 1) {
            Write-Warning "$($group.Group[0].BaseName): several files share this stem (e.g. .doc and .docx); skipped, keep one"
            $failed++
            continue
        }
        $file = $group.Group[0]
        $pdf = Join-Path $target ($file.BaseName + '.pdf')
        if ((Test-Path -LiteralPath $pdf) -and -not $Force) {
            Write-Warning "$($file.Name): $pdf exists; skipped (a new version takes a new file name, or pass -Force)"
            $failed++
            continue
        }
        if ($file.BaseName -notmatch $StemPattern) {
            Write-Warning "$($file.Name): the file stem is the doc id; use letters and digits joined by - _ or . (e.g. OPS-HANDBOOK-2026)"
        }
        $doc = $null
        try {
            # Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles)
            $doc = $word.Documents.Open($file.FullName, $false, $true, $false)
            $title = Get-DocumentTitle $doc
            if ($title -eq '') {
                Write-Warning "$($file.Name): empty Title property; ingest would take the first line of page 1 as the title"
            } elseif ($title.Contains(',')) {
                Write-Warning "$($file.Name): the Title contains a comma, which breaks citations; remove it"
            }
            if (-not (Test-HeadingStyleUsed $doc)) {
                Write-Warning "$($file.Name): no Heading 1-3 style in use; the PDF gets no bookmarks and citations no heading path"
            }
            # ExportAsFixedFormat(OutputFileName, ExportFormat, OpenAfterExport, OptimizeFor,
            #   Range, From, To, Item, IncludeDocProps, KeepIRM, CreateBookmarks,
            #   DocStructureTags, BitmapMissingFonts, UseISO19005_1)
            $doc.ExportAsFixedFormat($pdf, $wdExportFormatPDF, $false, $wdExportOptimizeForPrint,
                $wdExportAllDocument, 1, 1, $wdExportDocumentContent, $true, $true,
                $wdExportCreateHeadingBookmarks, $true, $true, $false)
            Write-Output "converted: $($file.Name) -> $pdf"
        } catch {
            Write-Warning "$($file.Name): conversion failed: $($_.Exception.Message)"
            $failed++
        } finally {
            if ($null -ne $doc) {
                $doc.Close($wdDoNotSaveChanges)
                [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($doc)
            }
        }
    }
} finally {
    $word.Quit($wdDoNotSaveChanges)
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word)
}

if ($failed -gt 0) { exit 1 }
exit 0
