#!/bin/sh
#lipai@mail.ustc.edu.cn

begin=$1
if [ $begin =='' ]; then begin=0; fi    
awk -v begin=$begin '/E0/{if ( i<begin  ) i++;else print $0 }' OSZICAR >temp.e
    
awk '/POSITION/,/drift/{
    if(NF==6) print $4,$5,$6;
    else if($1=="total") print $1 }' OUTCAR >temp.f

awk '{if($4=="F"||$4=="T") print $4,$5,$6}' CONTCAR >temp.fix
flag=`wc temp.fix|awk '{print $1}'`
steps=`grep E0 OSZICAR |tail -1 |awk '{print $1}'`
if [ flag != '0' ] ; then
    if [ -f temp.fixx ] ; then rm temp.fixx ; fi
    for i in `seq $steps`;do
        cat temp.fix >>temp.fixx
        echo >>temp.fixx
    done
    paste temp.f temp.fixx >temp.ff
fi

awk  '{ if($1=="total") {print ++i,a;a=0}
        else {
            if($4=="F") x=0; else x=$1;
            if($5=="F") y=0; else y=$2;
            if($6=="F") z=0; else z=$3;
            force=sqrt(x^2+y^2+z^2);
            if(a<force) a=force} }' temp.ff >force.conv
echo ""
echo -e "\033[1;36m=====================================================================================\033[0m"
gnuplot <<EOF 
set term dumb
set title 'Energy of each ionic step'
set xlabel 'Ionic steps'
set ylabel 'Energy(eV)'
plot 'temp.e' u 1:5 w l  t "Energy in eV"
set title 'Max Force of each ionic step'
set xlabel 'Ionic steps'
set ylabel 'Force (eV/Angstrom)'
plot 'force.conv' w l t "Force in eV/Angstrom"
EOF

if [ $steps -gt 8 ] ; then
tail -15 force.conv >temp.fff
tail -15 temp.e >temp.ee
echo ""
echo -e "\033[1;36m=====================================================================================\033[0m"
gnuplot <<EOF 
set term dumb
set title 'Energy of each ionic step for the last few steps'
set xlabel 'Ionic steps'
set ylabel 'Energy(eV)'
plot 'temp.ee' u 1:5 w l  t "Energy in eV"
set title 'Max Force of each ionic step for the last few steps'
set xlabel 'Ionic steps'
set ylabel 'Force (eV/Angstrom)'
plot 'temp.fff' w l  t "Force in eV/Angstrom"
EOF
rm temp.fff temp.ee
fi

# rm temp.e temp.f temp.ff temp.fix temp.fixx




## 修正后的版本，解决行粘连问题
if [ $steps -ge 15 ]; then
    echo ""
    echo -e "\033[1;36m=====================================================================================\033[0m"
    echo -e "\033[1;36m               Last 15 Steps Force Convergence Information\033[0m"
    echo -e "\033[1;36m=====================================================================================\033[0m"
    echo -e "\033[1;33m Step     Max Force (eV/Å)        Δ Force (eV/Å)         Trend\033[0m"
    echo -e "\033[1;36m-------------------------------------------------------------------------------------\033[0m"
    tail -15 force.conv | awk 'NR==1{prev=$2; 
                                  printf "    \033[1;37m%-6d   \033[1;32m%14.6f\033[0m        \033[1;35m%14.6f\033[0m        \033[1;33m%s\033[0m\n", 
                                  $1, $2, 0, "◼ Initial"; next} 
                              {delta=$2-prev; 
                               trend=(delta>0)?"▲ Increasing":(delta<0)?"▼ Decreasing":"▬ Stable";
                               color=(delta>0)?"\033[1;31m":"\033[1;34m";
                               printf "    \033[1;37m%-6d   \033[1;32m%14.6f\033[0m        %s%14.6f\033[0m        \033[1;33m%s\033[0m\n", 
                               $1, $2, color, delta, trend; 
                               prev=$2}'
    echo -e "\033[1;36m=====================================================================================\033[0m"
elif [ $steps -gt 0 ]; then
    echo ""
    echo -e "\033[1;36m=====================================================================================\033[0m"
    echo -e "\033[1;36m               Force Convergence Information (All $steps Steps)\033[0m"
    echo -e "\033[1;36m=====================================================================================\033[0m"
    echo -e "\033[1;33m Step     Max Force (eV/Å)        Δ Force (eV/Å)         Trend\033[0m"
    echo -e "\033[1;36m-------------------------------------------------------------------------------------\033[0m"
    awk 'NR==1{prev=$2; 
               printf "    \033[1;37m%-6d   \033[1;32m%14.6f\033[0m        \033[1;35m%14.6f\033[0m        \033[1;33m%s\033[0m\n", 
               $1, $2, 0, "◼ Initial"; next} 
           {delta=$2-prev; 
            trend=(delta>0)?"▲ Increasing":(delta<0)?"▼ Decreasing":"▬ Stable";
            color=(delta>0)?"\033[1;31m":"\033[1;34m";
            printf "    \033[1;37m%-6d   \033[1;32m%14.6f\033[0m        %s%14.6f\033[0m        \033[1;33m%s\033[0m\n", 
            $1, $2, color, delta, trend; 
            prev=$2}' force.conv
    echo -e "\033[1;36m=====================================================================================\033[0m"
fi


rm temp.e temp.f temp.ff temp.fix temp.fixx