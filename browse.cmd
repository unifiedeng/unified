@echo off
rem Read any supplier's catalog in a real browser, from any folder.
rem Page-shaped data: what categories exist, what materials something is sold in,
rem what a filter can narrow to. For per-part JSON on McMaster, use `mcm` instead.
rem   browse grainger "flat washers"          search a supplier
rem   browse fastenal "socket head screw"     part rows with links
rem   browse mcmaster rods --facet Material   expand a filter facet fully
rem   browse https://site.com/catalog/ --json read any catalog URL
python "%~dp0supplier.py" %*
